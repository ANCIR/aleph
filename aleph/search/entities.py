import json
from pprint import pprint  # noqa

from werkzeug.datastructures import MultiDict

from aleph.core import url_for, get_es, get_es_index
from aleph.index import TYPE_ENTITY, TYPE_DOCUMENT
from aleph.search.util import authz_collections_filter, authz_sources_filter
from aleph.search.util import execute_basic, parse_filters
from aleph.search.fragments import match_all, filter_query, aggregate
from aleph.search.facets import convert_entity_aggregations
from aleph.util import latinize_text

DEFAULT_FIELDS = ['collections', 'name', 'summary', 'jurisdiction_code',
                  'description', '$schema']

# Scoped facets are facets where the returned facet values are returned such
# that any filter against the same field will not be applied in the sub-query
# used to generate the facet values.
OR_FIELDS = ['collections']


def entities_query(args, fields=None, facets=True):
    """Parse a user query string, compose and execute a query."""
    if not isinstance(args, MultiDict):
        args = MultiDict(args)
    text = args.get('q', '').strip()
    if text is None or not len(text):
        q = match_all()
    else:
        q = {
            "query_string": {
                "query": text,
                "fields": ['name^15', 'name_latin^5',
                           'terms^12', 'terms_latin^3',
                           'summary^10', 'summary_latin^7',
                           'description^5', 'description_latin^3'],
                "default_operator": "AND",
                "use_dis_max": True
            }
        }

    q = authz_collections_filter(q)
    filters = parse_filters(args)
    aggs = {}
    if facets:
        aggs = aggregate(q, args)
        aggs = facet_collection(q, aggs, filters)

    sort_mode = args.get('sort', '').strip().lower()
    if sort_mode == 'doc_count':
        sort = [{'doc_count': 'desc'}, '_score']
    elif sort_mode == 'alphabet':
        sort = [{'name': 'asc'}, '_score']
    else:
        sort = ['_score']

    return {
        'sort': sort,
        'query': filter_query(q, filters, OR_FIELDS),
        'aggregations': aggs,
        '_source': fields or DEFAULT_FIELDS
    }


def facet_collection(q, aggs, filters):
    aggs['scoped']['aggs']['collection'] = {
        'filter': {
            'query': filter_query(q, filters, OR_FIELDS, skip='collections')
        },
        'aggs': {
            'collection': {
                'terms': {'field': 'collections', 'size': 1000}
            }
        }
    }
    return aggs


def suggest_entities(args):
    """Auto-complete API."""
    text = args.get('prefix')
    options = []
    if text is not None and len(text.strip()):
        q = {'match_phrase_prefix': {'terms': text.strip()}}
        q = {
            'size': 5,
            'query': authz_collections_filter(q),
            '_source': ['name', '$schema', 'terms']
        }
        ref = latinize_text(text)
        result = get_es().search(index=get_es_index(), doc_type=TYPE_ENTITY,
                                 body=q)
        for res in result.get('hits', {}).get('hits', []):
            ent = res.get('_source')
            terms = [latinize_text(t) for t in ent.pop('terms', [])]
            ent['match'] = ref in terms
            ent['id'] = res.get('_id')
            options.append(ent)
    return {
        'text': text,
        'results': options
    }


def execute_entities_query(args, query, doc_counts=False):
    """Execute the query and return a set of results."""
    result, hits, output = execute_basic(TYPE_ENTITY, query)
    convert_entity_aggregations(result, output, args)
    sub_queries = []
    for doc in hits.get('hits', []):
        entity = doc.get('_source')
        entity['id'] = doc.get('_id')
        entity['score'] = doc.get('_score')
        entity['api_url'] = url_for('entities_api.view', id=doc.get('_id'))
        output['results'].append(entity)

        sq = {'term': {'entities.uuid': entity['id']}}
        sq = authz_sources_filter(sq)
        sq = {'size': 0, 'query': sq}
        sub_queries.append(json.dumps({}))
        sub_queries.append(json.dumps(sq))

    if doc_counts and len(sub_queries):
        res = get_es().msearch(index=get_es_index(),
                               doc_type=TYPE_DOCUMENT,
                               body='\n'.join(sub_queries))
        for (entity, res) in zip(output['results'], res.get('responses')):
            entity['doc_count'] = res.get('hits', {}).get('total')
    return output
