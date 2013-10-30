#! /usr/bin/python2
# vim: set fileencoding=utf-8
import matplotlib.cm
import scipy.io as sio
from outplot import outplot
from timeit import default_timer as clock
import json
import colour
import datetime
import pymongo
from shapely.geometry import Point, shape, mapping
from math import floor
# from sys import float_info
# EPSILON = 1000*float_info.epsilon
import fiona
import numpy as np
from scipy.spatial.distance import pdist, squareform
from operator import itemgetter

CSS = '#{} {{fill: {}; opacity: 0.5; stroke: {}; stroke-width: 0.25px;}}'
KARTO_CONFIG = {'bounds': {'data': [-122.4, 37.768, -122.38, 37.778],
                           'mode': 'bbox'},
                'layers': {},
                'proj': {'id': 'laea', 'lat0': 37.78, 'lon0': -122.45}}


def total_seconds(td):
    if hasattr(td, 'total_seconds'):
        # python 2.7
        return td.total_seconds()
    # I don't care about microsecond
    return td.seconds + td.days * 24 * 3600


def to_css_hex(color):
    r = '#'
    for i in color[:-1]:
        c = hex(int(255*i))[2:]
        if len(c) == 2:
            r += c
        else:
            r += '0' + c
    return r


def bbox_to_polygon(bbox, latitude_first=True):
    """Return a 5 points GeoJSON polygon based on the bottom left and upper
    right coordinates of bbox [lat_bl, long_bl, lat_ur, long_ur]
    (5 because the polygon needs to be closed, see:
    https://groups.google.com/d/msg/mongodb-user/OPouYFHS_zU/cS21L0XAMkkJ )
    >>> bbox_to_polygon([37, -122, 35, -120])
    {'type': 'Polygon', 'coordinates': [[[-122, 37], [-120, 37], [-120, 35], [-122, 35], [-122, 37]]]}
    """
    assert(len(bbox) == 4)
    if latitude_first:
        lat_bl, long_bl, lat_ur, long_ur = bbox
    else:
        long_bl, lat_bl, long_ur, lat_ur = bbox
    r = {}
    r['type'] = 'Polygon'
    r['coordinates'] = [[[long_bl, lat_bl], [long_ur, lat_bl],
                         [long_ur, lat_ur], [long_bl, lat_ur],
                         [long_bl, lat_bl]]]
    return r


def inside_bbox(bbox):
    return {'$geoWithin': {'$geometry': bbox_to_polygon(bbox)}}


def get_photo_url(p, size='z', webpage=False):
    web_url = u"http://www.flickr.com/photos/{}/{}".format(p['uid'], p['_id'])
    BASE = u"http://farm{}.staticflickr.com/{}/{}_{}_{}.jpg"
    url = BASE.format(p['farm'], p['server'], p['_id'], p['secret'], size)
    if webpage:
        return url, web_url
    return url


def tag_location(collection, tag, bbox, start, end, uploaded=False,
                 user_is_tourist=None):
    """Return a list of [long, lat] for each photo taken between start and end
    (or uploaded) in bbox which has tag. If a dictionnary of users is provided,
    it adds tourist status as [long, lat, is_tourist]"""
    query = {}
    field = {'loc': 1, '_id': 0}
    if user_is_tourist is not None:
        field['uid'] = 1
#    if bbox is None:
#        query['hint'] = 'sf'
#    else:
#        query['loc'] = inside_bbox(bbox)
    query['hint'] = 'sf'
    if tag is not None:
        query['tags'] = {'$in': [tag]}
    time_field = 'upload' if uploaded else 'taken'
    query[time_field] = {'$gt': start, '$lt': end}
    cursor = collection.find(query, field)
    if user_is_tourist is None:
        return map(lambda p: p['loc']['coordinates'], list(cursor))
    return map(lambda p: p['loc']['coordinates'] + [user_is_tourist[p['uid']]],
               list(cursor))


def tag_over_time(collection, tag, bbox, start, interval, user_status=None):
    now = datetime.datetime.now()
    if interval is None:
        interval = now - start
        num_period = 1
    else:
        num_period = total_seconds(now - start)
        num_period = int(num_period/total_seconds(interval))
    schema = {'geometry': 'Point', 'properties': {}}
    # schema = {'geometry': 'Point', 'properties': {'tourist': 'int'}}
    for i in range(num_period):
        places = map(lambda p: {'geometry': mapping(Point(p[0], p[1])),
                                'properties': {}},
                     # 'properties': {'tourist': int(p[2])}},
                     tag_location(collection, tag, bbox,
                                  start + i * interval,
                                  start + (i+1) * interval,
                                  False,
                                  user_status))
        print('{} - {}: {}'.format(start + i * interval,
                                   start + (i+1) * interval, len(places)))
        name = '{}_{}.shp'.format(tag, i+1)
        with fiona.collection(name, "w", "ESRI Shapefile", schema) as f:
            f.writerecords(places)


def k_split_bbox(bbox, k=2, offset=0):
    long_step = (bbox[3] - bbox[1])/k
    lat_step = (bbox[2] - bbox[0])/k
    assert(offset <= min(long_step, lat_step))
    hoffset = offset
    voffset = 0
    x = bbox[1]
    y = bbox[0]
    region = []
    while y + voffset < bbox[2] - 0.5*lat_step:
        while x + hoffset < bbox[3] - 0.5*long_step:
            region.append([x + hoffset, y + voffset,
                           x + hoffset + long_step, y + voffset + lat_step])
            x += long_step

        y += lat_step
        x = bbox[1]

    def coord2region(coords):
        longitude, latitude = coords
        # TODO handle correctly edge case
        x = longitude - hoffset - bbox[1] - 1e-8
        y = latitude - voffset - bbox[0] - 1e-8
        if x < 0 or y < 0:
            return -1
        r = k*int(floor(y/lat_step)) + int(floor(x/long_step))
        if r > k*k - 1:
            return - 1
        return r

    return region, coord2region


def compute_entropy(count):
    c = np.array([i for i in count if i > 0])
    N = np.sum(c)
    return np.log(N) - np.sum(c*np.log(c))/N


def compute_frequency(collection, tag, bbox, start, end, k=3,
                      nb_inter=3, exclude_zero=True, uploaded=False):
    """split bbox in k^2 rectangles and compute the frequency of tag in each of
    them. Return a list of list of Polygon, grouped by similar frequency
    into nb_inter bucket (potentialy omiting the zero one for clarity)."""
    coords = tag_location(collection, tag, bbox, start, end, uploaded)
    r, f = k_split_bbox(bbox, k)
    # count[0] is for potential points that do not fall in any region (it must
    # only happens because of rounding inprecision)
    count = (len(r)+1)*[0, ]
    for loc in coords:
        count[f(loc)+1] += 1

    N = len(coords)
    sio.savemat('distrib', {'c': np.array(count[1:])})
    entropy = compute_entropy(count[1:])
    print("Entropy of {}: {:.4f}".format(tag, entropy))
    freq = np.array(count[1:])/(1.0*N)
    log_freq = np.maximum(0, np.log(count[1:]))
    maxv = np.max(log_freq)
    minv = np.min(log_freq)
    interval_size = (maxv - minv)/nb_inter
    bucket = []
    for i in range(nb_inter):
        bucket.append([])
    for i, v in enumerate(log_freq):
        if not (exclude_zero and v < 1e-8):
            poly = {'geometry': mapping(shape(bbox_to_polygon(r[i], False))),
                    'properties': {}}
            index = (min(nb_inter - 1, int(floor(v / interval_size))))
            bucket[index].append(poly)
    return bucket, minv, maxv


def plot_polygons(bucket, tag, nb_inter, minv, maxv):
    schema = {'geometry': 'Polygon', 'properties': {}}
    colormap_ = matplotlib.cm.ScalarMappable(cmap='YlOrBr')
    colormap = [to_css_hex(c) for c in colormap_.to_rgba(np.linspace(minv, maxv, nb_inter))]
    style = []
    for i in range(nb_inter):
        if len(bucket[i]) > 0:
            name = '{}_freq_{}'.format(tag, i+1)
            KARTO_CONFIG['layers'][name] = {'src': name+'.shp'}
            style.append(CSS.format(name, colormap[i], colormap[i]))
            with fiona.collection(name+'.shp', "w",
                                  "ESRI Shapefile", schema) as f:
                f.writerecords(bucket[i])

    with open('photos.json', 'w') as f:
        json.dump(KARTO_CONFIG, f)
    with open('photos.css', 'w') as f:
        f.write('\n'.join(style))


def simple_metrics(collection, tag, bbox, start, end):
    places = tag_location(collection, tag, bbox, start, end)
    p = np.array(zip(map(itemgetter(1), places), map(itemgetter(0), places)))
    grav = np.mean(p, 0)
    tmp = p - grav
    dst = np.sum(tmp**2, 1)
    outplot(tag + '_grav.dat', [''], dst)

    dst = pdist(p)
    h, b = np.histogram(dst, 200)
    outplot(tag + '_pairwise.dat', ['', ''], h, b)

    pd = squareform(dst)
    np.fill_diagonal(pd, 1e6)
    dst = np.min(pd, 1)
    outplot(tag + '_neighbor.dat', [''], dst)


def get_user_status(collection):
    users = list(collection.find(fields={'tourist': 1}))
    return dict([(u['_id'], u['tourist']) for u in users])


def classify_users(db):
    users_collection = db['users']
    users_from_photos = db['photos'].aggregate([
        {'$project': {'_id': 0, 'upload': 1, 'user': '$uid'}},
        {'$group': {'_id': '$user',
                    'first': {'$min': '$upload'},
                    'last': {'$max': '$upload'},
                    "count": {"$sum": 1}}}
    ])['result']
    month = total_seconds(datetime.timedelta(days=365.24/12))
    users = []
    autochthons = 0
    for u in users_from_photos:
        timespan = total_seconds(u['last'] - u['first'])
        u['tourist'] = timespan < month
        if not u['tourist']:
            autochthons += 1
        users.append(u)
    print(100*autochthons/len(users))
    try:
        users_collection.insert(users, continue_on_error=True)
    except pymongo.errors.DuplicateKeyError:
        print('duplicate')
        pass


if __name__ == '__main__':
    start = clock()
    client = pymongo.MongoClient('localhost', 27017)
    db = client['flickr']
    photos = db['photos']
    SF_BBOX = [37.7123, -122.531, 37.84, -122.35]
    KARTO_CONFIG['bounds']['data'] = [SF_BBOX[1], SF_BBOX[0],
                                      SF_BBOX[3], SF_BBOX[2]]
    # import doctest
    # doctest.testmod()
    nb_inter = 20
    b, minv, maxv = compute_frequency(photos, None, SF_BBOX,
                          datetime.datetime(2008, 1, 1),
                          datetime.datetime(2014, 1, 1), 160, nb_inter)
    plot_polygons(b, '_distrib', nb_inter, minv, maxv)
    # classify_users(db, photos)
    # u = get_user_status(db['users'])
    # tag_over_time(photos, 'local', None,
    #               datetime.datetime(2005, 1, 1),
    #               datetime.timedelta(days=3218), u)
    # simple_metrics(photos, 'street',
    #         SF_BBOX,
    #         datetime.datetime(2008, 1, 1),
    #         datetime.datetime(2014, 1, 1))
    # simple_metrics(photos, 'museum',
    #                SF_BBOX,
    #                datetime.datetime(2008, 1, 1),
    #                datetime.datetime(2014, 1, 1))
    t = 1000*(clock() - start)
    print('aggregate in {:.3f}ms'.format(t))
