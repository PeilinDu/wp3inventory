from flask import Blueprint, render_template, request, redirect, url_for, abort, current_app
from flaskinventory import dgraph
from flaskinventory.view.forms import SimpleQuery

main = Blueprint('main', __name__)


@main.route('/')
@main.route('/home')
def home():
    # needs caching!
    countries = dgraph.query('''{ q(func: type("Country")) { name uid } }''')
    c_choices = [(country.get('uid'), country.get('name'))
                 for country in countries['q']]
    c_choices = sorted(c_choices, key=lambda x: x[1])
    c_choices.insert(0, ('all', 'All'))
    form = SimpleQuery()
    form.country.choices = c_choices

    query_string = f'''{{
                        data(func: has(dgraph.type), orderdesc: creation_date, first: 5) 
                            @normalize @filter(eq(entry_review_status, "accepted") AND has(creation_date)) {{
                                uid
                                unique_name: unique_name 
                                name: name 
                                type: dgraph.type 
                                title: title
                                creation_date: creation_date
                                channel {{ channel: name }}
                                country {{ country: name }}
                            }}
                        }}'''
    
    result = dgraph.query(query_string)

    return render_template('home.html', form=form, recent=result['data'], show_sidebar=True)


@main.route('/about')
@main.route('/about/')
def about():
    return render_template('main/about.html', title="About Page")

@main.route('/imprint')
def imprint():
    return render_template('main/imprint.html')

@main.route('/privacy')
def privacy():
    return render_template('main/privacy.html')

@main.route('/notimplemented')
def under_development():
    return render_template('not_implemented.html')

@main.route('/guides/newssource')
def guides_newssource():
    return render_template('guides/newsource.html')