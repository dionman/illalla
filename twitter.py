﻿#! /usr/bin/python2
# vim: set fileencoding=utf-8
"""Retrieve checkins tweets"""
import TwitterAPI as twitter
from api_keys import TWITTER_CONSUMER_KEY as consumer_key
from api_keys import TWITTER_CONSUMER_SECRET as consumer_secret
from api_keys import TWITTER_ACCESS_TOKEN as access_token
from api_keys import TWITTER_ACCESS_SECRET as access_secret
import read_foursquare as rf
CITIES_TREE = rf.obtain_tree()
from utils import get_nested
import cities
import locale
locale.setlocale(locale.LC_ALL, 'C')  # to parse date
UTC_DATE = '%a %b %d %X +0000 %Y'
UTC_TZ = cities.pytz.utc


def parse_tweet(tweet):
    """Return a CheckIn from `tweet` or None if it is not located in a valid
    city"""
    loc = get_nested(tweet, 'coordinates')
    city = None
    if not loc:
        # In that case, we would have to follow the link to know whether the
        # checkin falls within our cities but that's too costly so we drop it
        # (and introduce a bias toward open sharing users I guess)
        return None
    lon, lat = loc['coordinates']
    city = rf.find_town(lat, lon, CITIES_TREE)
    if not (city and city in cities.SHORT_KEY):
        return None
    id_ = get_nested(tweet, 'id_str')
    urls = get_nested(tweet, ['entities', 'urls'], [])
    # short url of the checkin that need to be expand, either using bitly API
    # or by VenueIdCrawler. Once we get the full URL, we still need to request
    # 4SQ (500 per hours) to get info (or look at page body, which contains the
    # full checkin in a javascript field)
    lid = [url['expanded_url'] for url in urls
           if '4sq.com' in url['expanded_url']][0]
    uid = get_nested(tweet, ['user', 'id_str'])
    try:
        time = rf.datetime.strptime(tweet['created_at'], UTC_DATE)
        time = time.replace(tzinfo=UTC_TZ)
        time = cities.TZ[city].normalize(time.astimezone(cities.TZ[city]))
        #TODO remove tz info once we got local time
    except ValueError:
        print('time: {}'.format(tweet['created_at']))
        time = None
    return rf.CheckIn(id_, lid, uid, city, loc, time)


if __name__ == '__main__':
    #pylint: disable=C0103
    api = twitter.TwitterAPI(consumer_key, consumer_secret,
                             access_token, access_secret)
    req = api.request('statuses/filter', {'track': '4sq com'})
    i = 0
    r = 0
    for item in req.get_iterator():
        c = parse_tweet(item)
        i += 1
        if c:
            print(c)
            r += 1
        if r > 2:
            print(i)
            break
