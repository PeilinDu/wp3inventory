import datetime
from typing import Union
from flask import current_app
from flaskinventory import dgraph
from flaskinventory.errors import InventoryPermissionError, InventoryValidationError
from flaskinventory.flaskdgraph.dgraph_types import (MutualListRelationship, String, Integer, Boolean, UIDPredicate,
                                                     SingleChoice, MultipleChoice,
                                                     DateTime, Year, GeoScalar,
                                                     ListString, ListRelationship,
                                                     Geo, SingleRelationship, UniqueName,
                                                     ReverseRelationship, ReverseListRelationship, NewID, UID)


from flaskinventory.add.external import geocode, reverse_geocode, get_wikidata
from flaskinventory.users.constants import USER_ROLES
from flaskinventory.flaskdgraph import Schema
from flaskinventory.flaskdgraph.utils import validate_uid
from flaskinventory.auxiliary import icu_codes

from slugify import slugify
import secrets

"""
    Custom Fields
"""


class GeoAutoCode(Geo):

    autoinput = 'address_string'

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)

    def autocode(self, data, **kwargs) -> Union[GeoScalar, None]:
        return self.str2geo(data)


class AddressAutocode(Geo):

    autoinput = 'name'

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)

    def validation_hook(self, data):
        return str(data)

    def autocode(self, data: str, **kwargs) -> Union[dict, None]:
        query_result = self.str2geo(data)
        geo_result = None
        if query_result:
            geo_result = {'address_geo': query_result}
            address_lookup = reverse_geocode(
                query_result.lat, query_result.lon)
            geo_result['address_string'] = address_lookup.get('display_name')
        return geo_result


class SourceCountrySelection(ListRelationship):

    """
        Special field with constraint to only include countries with Scope of OPTED
    """

    def __init__(self, *args, **kwargs) -> None:

        super().__init__(relationship_constraint = ['Multinational', 'Country'], 
                            allow_new=False, autoload_choices=True, 
                            overwrite=True, *args, **kwargs)

    def get_choices(self):

        query_country = '''country(func: type("Country"), orderasc: name) @filter(eq(opted_scope, true)) { uid unique_name name  }'''
        query_multinational = '''multinational(func: type("Multinational"), orderasc: name) { uid unique_name name other_names }'''

        query_string = '{ ' + query_country + query_multinational + ' }'

        choices = dgraph.query(query_string=query_string)

        if len(self.relationship_constraint) == 1:
            self.choices = {c['uid']: c['name'] for c in choices[self.relationship_constraint[0].lower()]}
            self.choices_tuples = [(c['uid'], c['name']) for c in choices[self.relationship_constraint[0].lower()]]

        else:
            self.choices = {}
            self.choices_tuples = {}
            for dgraph_type in self.relationship_constraint:
                self.choices_tuples[dgraph_type] = [(c['uid'], c['name']) for c in choices[dgraph_type.lower()]]
                self.choices.update({c['uid']: c['name'] for c in choices[dgraph_type.lower()]})


class SubunitAutocode(ListRelationship):

    def __init__(self, *args, **kwargs) -> None:

        super().__init__(relationship_constraint = ['Subunit'], 
                            allow_new=True, autoload_choices=True, 
                            overwrite=True, *args, **kwargs)

    def get_choices(self):

        query_string = '''{ subunit(func: type("Subunit"), orderasc: name) { uid unique_name name  } }'''

        choices = dgraph.query(query_string=query_string)

        if len(self.relationship_constraint) == 1:
            self.choices = {c['uid']: c['name'] for c in choices[self.relationship_constraint[0].lower()]}
            self.choices_tuples = [(c['uid'], c['name']) for c in choices[self.relationship_constraint[0].lower()]]

        else:
            self.choices = {}
            self.choices_tuples = {}
            for dgraph_type in self.relationship_constraint:
                self.choices_tuples[dgraph_type] = [(c['uid'], c['name']) for c in choices[dgraph_type.lower()]]
                self.choices.update({c['uid']: c['name'] for c in choices[dgraph_type.lower()]})

    def _geo_query_subunit(self, query):
        geo_result = geocode(query)
        if geo_result:
            dql_string = f'''{{ q(func: eq(country_code, "{geo_result['address']['country_code']}")) @filter(type("Country")) {{ uid }} }}'''
            dql_result = dgraph.query(dql_string)
            try:
                country_uid = dql_result['q'][0]['uid']
            except Exception:
                raise InventoryValidationError(
                    f"Error in <{self.predicate}>! While parsing {query} no matching country found in inventory: {geo_result['address']['country_code']}")
            geo_data = GeoScalar('Point', [
                float(geo_result.get('lon')), float(geo_result.get('lat'))])

            name = None
            other_names = [query]
            if geo_result['namedetails'].get('name'):
                other_names.append(geo_result['namedetails'].get('name'))
                name = geo_result['namedetails'].get('name')

            if geo_result['namedetails'].get('name:en'):
                other_names.append(geo_result['namedetails'].get('name:en'))
                name = geo_result['namedetails'].get('name:en')

            other_names = list(set(other_names))

            if not name:
                name = query

            if name in other_names:
                other_names.remove(name)

            new_subunit = {'name': name,
                           'country': UID(country_uid),
                           'other_names': other_names,
                           'location_point': geo_data,
                           'country_code': geo_result['address']['country_code']}

            if geo_result.get('extratags'):
                if geo_result.get('extratags').get('wikidata'):
                    if geo_result.get('extratags').get('wikidata').lower().startswith('q'):
                        try:
                            new_subunit['wikidataID'] = int(geo_result.get(
                                'extratags').get('wikidata').lower().replace('q', ''))
                        except Exception as e:
                            current_app.logger.debug(
                                f'<{self.predicate}>: Could not parse wikidata ID in subunit "{query}": {e}')

            return new_subunit
        else:
            return False

    def _resolve_subunit(self, subunit):
        geo_query = self._geo_query_subunit(subunit)
        if geo_query:
            geo_query['dgraph.type'] = ['Subunit']
            # prevent duplicates
            geo_query['unique_name'] = f"{slugify(subunit, separator='_')}_{geo_query['country_code']}"
            duplicate_check = dgraph.get_uid(
                'unique_name', geo_query['unique_name'])
            if duplicate_check:
                geo_query = {'uid': UID(duplicate_check)}
            else:
                geo_query['uid'] = NewID(
                    f"_:{slugify(secrets.token_urlsafe(8))}")
            return geo_query
        else:
            raise InventoryValidationError(
                f'Invalid Data! Could not resolve geographic subunit {subunit}')

    def validation_hook(self, data):
        uid = validate_uid(data)
        if not uid:
            if not self.allow_new:
                raise InventoryValidationError(
                    f'Error in <{self.predicate}>! provided value is not a UID: {data}')
            new_subunit = self._resolve_subunit(data)
            return new_subunit
        if self.relationship_constraint:
            entry_type = dgraph.get_dgraphtype(uid)
            if entry_type not in self.relationship_constraint:
                raise InventoryValidationError(
                    f'Error in <{self.predicate}>! UID specified does not match constrain, UID is not a {self.relationship_constraint}!: uid <{uid}> <dgraph.type> <{entry_type}>')        
        return {'uid': UID(uid)}
        

class OrganizationAutocode(ReverseListRelationship):

    def __init__(self, predicate_name, *args, **kwargs) -> None:

        super().__init__(predicate_name,
                            relationship_constraint = ['Organization'], 
                            allow_new=True, 
                            autoload_choices=False, 
                            overwrite=True, 
                            *args, **kwargs)

    def validation_hook(self, data, node, facets=None):
        uid = validate_uid(data)
        if not uid:
            if not self.allow_new:
                raise InventoryValidationError(
                    f'Error in <{self._predicate}>! provided value is not a UID: {data}')
            new_org = {'uid': NewID(data, facets=facets), self._target_predicate: node, 'name': data}
            new_org = self._resolve_org(new_org)
            if self.default_predicates:
                new_org.update(self.default_predicates)
            return new_org
        if self.relationship_constraint:
            entry_type = dgraph.get_dgraphtype(uid)
            if entry_type not in self.relationship_constraint:
                raise InventoryValidationError(
                    f'Error in <{self._predicate}>! UID specified does not match constrain, UID is not a {self.relationship_constraint}!: uid <{uid}> <dgraph.type> <{entry_type}>')        
        return {'uid': UID(uid, facets=facets), self._target_predicate: node}

    def validate(self, data, node, facets=None) -> Union[UID, NewID, dict]:
        if isinstance(data, str):
            data = data.split(',')

        data = set([item.strip() for item in data])
        uids = []

        for item in data:
            uid = self.validation_hook(item, node, facets=facets)
            uids.append(uid)
        
        return uids

    def _resolve_org(self, org):

        geo_result = geocode(org['name'])
        if geo_result:
            try:
                org['address_geo'] = GeoScalar('Point', [
                    float(geo_result.get('lon')), float(geo_result.get('lat'))])
            except:
                pass
            try:
                address_lookup = reverse_geocode(
                    geo_result.get('lat'), geo_result.get('lon'))
                org['address_string'] = address_lookup['display_name']
            except:
                pass

        wikidata = get_wikidata(org['name'])

        if wikidata:
            for key, val in wikidata.items():
                if key not in org.keys():
                    org[key] = val
        
        if self.relationship_constraint:
            org['dgraph.type'] = self.relationship_constraint

        return org




"""
    Entry
"""


class Entry(Schema):

    uid = UIDPredicate()
    
    unique_name = UniqueName()
    
    name = String(required=True)
    
    other_names = ListString(overwrite=True)
    
    entry_notes = String(description='Do you have any other notes on the entry that you just coded?',
                         large_textfield=True)
    
    wikidataID = Integer(label='WikiData ID',
                         overwrite=True,
                         new=False)
    
    entry_review_status = SingleChoice(choices={'draft': 'Draft',
                                                'pending': 'Pending',
                                                'accepted': 'Accepted',
                                                'rejected': 'Rejected'},
                                       default='pending',
                                       required=True,
                                       new=False,
                                       permission=USER_ROLES.Reviewer)


class Organization(Entry):

    name = String(label='Organization Name',
                  required=True,
                  description='What is the legal or official name of the media organisation?',
                  render_kw={'placeholder': 'e.g. The Big Media Corp.'})
    
    other_names = ListString(description='Does the organisation have any other names or common abbreviations?',
                             render_kw={'placeholder': 'Separate by comma'}, 
                             overwrite=True)
    
    is_person = Boolean(description='Is the media organisation a person?',
                        default=False)
    
    ownership_kind = SingleChoice(choices={
                                    'NA': "Don't know / NA",
                                    'public ownership': 'Mainly public ownership',
                                    'private ownership': 'Mainly private Ownership',
                                    'political party': 'Political Party',
                                    'unknown': 'Unknown Ownership'},
                                  description='Is the media organization mainly privately owned or publicly owned?')

    country = SingleRelationship(relationship_constraint='Country', 
                                 allow_new=False,
                                 overwrite=True, 
                                 description='In which country is the organisation located?')
    
    publishes = ListRelationship(allow_new=False, 
                                 relationship_constraint='Source', 
                                 overwrite=True, 
                                 description='Which news sources publishes the organisation (or person)?',
                                 render_kw={'placeholder': 'Type to search existing news sources and add multiple...'})
    
    owns = ListRelationship(allow_new=False,
                            relationship_constraint='Organization',
                            overwrite=True,
                            description='Which other media organisations are owned by this new organisation (or person)?',
                            render_kw={'placeholder': 'Type to search existing organisations and add multiple...'})

    party_affiliated = SingleChoice(choices={
                                        'NA': "Don't know / NA",
                                        'yes': 'Yes',
                                        'no': 'No'
                                    })
    
    address_string = AddressAutocode(new=False,
                                     render_kw={'placeholder': 'Main address of the organization.'})
    
    address_geo = GeoAutoCode(read_only=True, new=False, hidden=True)
    
    employees = String(description='How many employees does the news organization have?',
                       render_kw={
                           'placeholder': 'Most recent figure as plain number'},
                       new=False)
    
    founded = DateTime(new=False)


class Resource(Entry):

    description = String(large_textfield=True)
    authors = ListString(render_kw={'placeholder': 'Separate by comma'})
    published_date = DateTime()
    last_updated = DateTime()
    url = String()
    doi = String()
    arxiv = String()

class Archive(Resource):

    access = SingleChoice(choices={'free': 'Free',
                                    'restricted': 'Restricted'})
    sources_included = ListRelationship(relationship_constraint='Source', allow_new=False)
    fulltext = Boolean(description='Dataset contains fulltext')
    country = ListRelationship(relationship_constraint=['Country', 'Multinational'])


class Source(Entry):

    channel = SingleRelationship(description='Through which channel is the news source distributed?',
                                 edit=False,
                                 allow_new=False,
                                 autoload_choices=True,
                                 relationship_constraint='Channel',
                                 read_only=True,
                                 required=True)

    name = String(label='Name of the News Source',
                  required=True,
                  description='What is the name of the news source?',
                  render_kw={'placeholder': "e.g. 'The Royal Gazette'"})
    
    other_names = ListString(description='Is the news source known by alternative names (e.g. Krone, Die Kronen Zeitung)?',
                             render_kw={'placeholder': 'Separate by comma'}, 
                             overwrite=True)

    founded = Year(description="What year was the print news source founded?")

    publication_kind = MultipleChoice(description='What label or labels describe the main source?',
                                      choices={'newspaper': 'Newspaper / News Site', 
                                                'news agency': 'News Agency', 
                                                'magazine': 'Magazine', 
                                                'tv show': 'TV Show / TV Channel', 
                                                'radio show': 'Radio Show / Radio Channel', 
                                                'podcast': 'Podcast', 
                                                'news blog': 'News Blog', 
                                                'alternative media': 'Alternative Media'},
                                        tom_select=True,
                                        required=True)

    special_interest = Boolean(description='Does the news source have one main topical focus?',
                                label='Yes, is a special interest publication')
    
    topical_focus = MultipleChoice(description="What is the main topical focus of the news source?",
                                    choices={'politics': 'Politics', 
                                             'society': 'Society & Panorama', 
                                             'economy': 'Business, Economy, Finance & Stocks', 
                                             'religion': 'Religion', 
                                             'science': 'Science & Technology', 
                                             'media': 'Media', 
                                             'environment': 'Environment', 
                                             'education': 'Education'},
                                    tom_select=True)
    
    publication_cycle = SingleChoice(description="What is the publication cycle of the source?",
                                        choices={'continuous': 'Continuous', 
                                                 'daily': 'Daily (7 times a week)', 
                                                 'multiple times per week': 'Multiple times per week', 
                                                 'weekly': 'Weekly', 
                                                 'twice a month': 'Twice a month', 
                                                 'monthly': 'Monthly', 
                                                 'less than monthly': 'Less frequent than monthly', 
                                                 'NA': "Don't Know / NA"},
                                                 required=True)

    publication_cycle_weekday = MultipleChoice(description="Please indicate the specific day(s)",
                                                choices={1: 'Monday', 
                                                        2: 'Tuesday', 
                                                        3: 'Wednesday', 
                                                        4: 'Thursday', 
                                                        5: 'Friday', 
                                                        6: 'Saturday', 
                                                        7: 'Sunday', 
                                                        'NA': "Don't Know / NA"},
                                                tom_select=True)

    geographic_scope = SingleChoice(description="What is the geographic scope of the news source?",
                                    choices={'multinational': 'Multinational', 
                                             'national': 'National', 
                                             'subnational': 'Subnational', 
                                             'NA': "Don't Know / NA"},
                                    required=True,
                                    radio_field=True)

    country = SourceCountrySelection(label='Countries', 
                                        description='Which countries are in the geographic scope?',
                                        required=True)

    geographic_scope_subunit = SubunitAutocode(label='Subunits',
                                                description='What is the subnational scope?',
                                                tom_select=True)

    languages = MultipleChoice(description="In which language(s) does the news source publish its news texts?",
                                required=True,
                                choices=icu_codes,
                                tom_select=True)

    payment_model = SingleChoice(description="Is the content produced by the news source accessible free of charge?",
                                    choices={'free': 'Free, all content is free of charge', 
                                            'partly free': 'Some content is free of charge', 
                                            'not free': 'No content is free of charge', 
                                            'NA': "Don't Know / NA"},
                                    required=True,
                                    radio_field=True)

    contains_ads = SingleChoice(description="Does the news source contain advertisements?",
                                    choices={'yes': 'Yes', 
                                                'no': 'No', 
                                                'non subscribers': 'Only for non-subscribers', 
                                                'NA': "Don't Know / NA"},
                                    required=True,
                                    radio_field=True)

    audience_size = Year(default=datetime.date.today())

    publishes_org = OrganizationAutocode('publishes', 
                                       label='Published by',
                                       default_predicates={'is_person': False})

    publishes_person = OrganizationAutocode('publishes', 
                                       label='Published by person',
                                       default_predicates={'is_person': True})

    archive_sources_included = ReverseListRelationship('sources_included', 
                                                    allow_new=False, 
                                                    relationship_constraint='Archive',
                                                    label='News source included in these resources')

    related = MutualListRelationship(allow_new=True, autoload_choices=False, relationship_constraint='Source')



class Resource(Entry):

    description = String(large_textfield=True)
    authors = ListString(render_kw={'placeholder': 'Separate by comma'})
    published_date = DateTime()
    last_updated = DateTime()
    url = String()
    doi = String()
    arxiv = String()

class Archive(Resource):

    access = SingleChoice(choices={'free': 'Free',
                                    'restricted': 'Restricted'})
    sources_included = ListRelationship(relationship_constraint='Source', allow_new=False)
    fulltext = Boolean(description='Dataset contains fulltext')
    country = ListRelationship(relationship_constraint=['Country', 'Multinational'])

# entry_countries = ListRelationship(
#     'country', relationship_constraint='Country', allow_new=False, overwrite=True)


