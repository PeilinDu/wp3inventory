from flask import (Blueprint, render_template, url_for,
                   flash, redirect, request, abort, jsonify)
from flask_login import current_user, login_required
from flaskinventory import dgraph
from flaskinventory.flaskdgraph.dgraph_types import SingleChoice
from flaskinventory.flaskdgraph import Schema, build_query_string
from flaskinventory.flaskdgraph.query import generate_query_forms
from flaskinventory.users.constants import USER_ROLES
from flaskinventory.users.utils import requires_access_level
from flaskinventory.view.dgraph import (get_entry, get_rejected)
from flaskinventory.view.utils import can_view
from flaskinventory.flaskdgraph.utils import validate_uid, restore_sequence
from flaskinventory.review.utils import create_review_actions
from flaskinventory.misc.utils import validate_doi

view = Blueprint('view', __name__)


@view.route('/search')
def search():
    if request.args.get('query'):
        search_terms = request.args.get('query')
        if validate_uid(search_terms):
            return view_uid(uid=validate_uid(search_terms))

        return redirect(url_for('view.query', _terms=search_terms))
    else:
        flash("Please enter a search query in the top search bar", "info")
        return redirect(url_for('main.home'))


@view.route("/view")
@view.route("/view/uid/<string:uid>")
def view_uid(uid=None):
    request_args = request.args.to_dict()
    if request_args.get('uid'):
        uid = request_args.pop('uid')

    uid = validate_uid(uid)
    if not uid:
        return abort(404)
    dgraphtype = dgraph.get_dgraphtype(uid)
    if dgraphtype:
        if dgraphtype.lower() == 'rejected':
            return redirect(url_for('view.view_rejected', uid=uid))
        return redirect(url_for('view.view_generic', dgraph_type=dgraphtype, uid=uid, **request_args))
    else:
        return abort(404)


@login_required
@view.route("/view/rejected/<uid>")
def view_rejected(uid):
    uid = validate_uid(uid)
    if not uid:
        flash('Invalid ID provided!', "warning")
        return abort(404)

    data = get_rejected(uid)

    if not data:
        return abort(404)

    if not can_view(data, current_user):
        return abort(403)

    return render_template('view/rejected.html',
                           title=f"Rejected: {data.get('name')}",
                           entry=data)


@view.route("/view/<string:dgraph_type>/uid/<uid>")
@view.route("/view/<string:dgraph_type>/<string:unique_name>")
def view_generic(dgraph_type=None, uid=None, unique_name=None):
    dgraph_type = Schema.get_type(dgraph_type)
    if not dgraph_type:
        if uid:
            return redirect(url_for('view.view_uid', uid=uid))
        else:
            return abort(404)

    data = get_entry(uid=uid, unique_name=unique_name, dgraph_type=dgraph_type)

    if not data:
        return abort(404)

    if any(x in data['dgraph.type'] for x in ['Source', 'Organization']):
        show_sidebar = True
    else:
        show_sidebar = False

    # pretty printing
    fields = Schema.get_predicates(dgraph_type)
    for key, v in data.items():
        if key in fields:
            try:
                if isinstance(fields[key], SingleChoice):
                    if isinstance(v, list):
                        data[key] = [fields[key].choices[subval]
                                     for subval in v]
                        data[key].sort()
                    else:
                        data[key] = fields[key].choices[v]
            except KeyError:
                pass

    review_actions = create_review_actions(
        current_user, data['uid'], data['entry_review_status'])
    return render_template('view/generic.html',
                           title=data.get('name'),
                           entry=data,
                           dgraph_type=dgraph_type,
                           review_actions=review_actions,
                           show_sidebar=show_sidebar)


@view.route("/query", methods=['GET', 'POST'])
def query():
    if request.method == 'POST':
        r = {}
        for k, v in request.form.to_dict(flat=False).items():
            # remove operators if field not selected
            if '*' in k:
                if k.split('*')[0] not in request.form.to_dict():
                    continue
            # remove empty fields
            if v[0] != '':
                r[k] = v
        try:
            # in testing environments CSRF tokens are disabled
            r.pop('csrf_token')
            r.pop('submit')
        except:
            pass
        return redirect(url_for("view.query", **r))
    total = None
    result = None
    pages = 1
    r = {k: v for k, v in request.args.to_dict(
        flat=False).items() if v[0] != ''}
    try:
        json_output = r.pop('json')
    except:
        json_output = False
    if len(r) > 0:
        query_string = build_query_string(r)
        if query_string:
            search_terms = request.args.get('_terms', '')
            if not search_terms == '':
                variables = {'$searchTerms': search_terms}
            else:
                variables = None
            result = dgraph.query(query_string, variables=variables)
            total = result['total'][0]['count']

            max_results = int(request.args.get('_max_results', 25))
            # make sure no random values are passed in as parameters
            if not max_results in [10, 25, 50]:
                max_results = 25

            # fancy ceiling division
            pages = -(total // -max_results)

            result = result['q']

            # clean 'Entry' from types
            if len(result) > 0:
                for item in result:
                    if 'Entry' in item['dgraph.type']:
                        item['dgraph.type'].remove('Entry')
                    if any(t in item['dgraph.type'] for t in ['ResearchPaper', 'Tool', 'Corpus', 'Dataset']):
                        restore_sequence(item)

    r_args = {k: v for k, v in request.args.to_dict(
        flat=False).items() if v[0] != ''}
    try:
        current_page = int(r_args.pop('_page')[0])
    except:
        current_page = 1

    form = generate_query_forms(dgraph_types=['Source', 'Organization', 'Tool', 'Archive', 'Dataset', 'Corpus'],
                                populate_obj=request.args)

    if json_output:
        j_result = {'_status': 200, 
                    '_page': current_page, 
                    '_total_pages': pages, 
                    '_total_results': total or 0, 
                    'result': result or []}
        return jsonify(j_result)
    return render_template("query/index.html", form=form, result=result, r_args=r_args, total=total, pages=pages, current_page=current_page)


@view.route("/query/json")
@login_required
@requires_access_level(USER_ROLES.Admin)
def query_json():
    public = request.args.get('_public', True)
    if isinstance(public, str):
        if public == 'False':
            public = False
    query_string = build_query_string(
        request.args.to_dict(flat=False), public=public)

    result = dgraph.query(query_string)

    return jsonify(result)
