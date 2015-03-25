from base import TrackProvider
import requests
import gnupg
import re
import pytz
import logging
import json
import math
from datetime import datetime, timedelta, time
from django.utils.timezone import utc
from django.contrib.gis.geos import Point
from sport.models import Sport
from tracks.models import TrackSplit
from django.utils.timezone import make_aware

logger = logging.getLogger('coach.sport.garmin')

class GarminAuthException(Exception):
  '''
  Used when the specific garmin login process fails
  '''
  pass

class GarminProvider(TrackProvider):
  NAME = 'garmin'
  settings = ['GPG_HOME', 'GPG_PASSPHRASE', ]

  # Login Urls
  url_hostname = 'http://connect.garmin.com/gauth/hostname'
  url_login = 'https://sso.garmin.com/sso/login'
  url_post_login = 'http://connect.garmin.com/post-auth/login'
  url_check_login = 'http://connect.garmin.com/user/username'

  # Data Urls
  url_activity = 'http://connect.garmin.com/proxy/activity-search-service-1.0/json/activities'
  url_laps = 'http://connect.garmin.com/proxy/activity-service-1.3/json/activity/%s'
  url_details = 'http://connect.garmin.com/proxy/activity-service-1.3/json/activityDetails/%s'

  def auth(self, force_login=None, force_password=None):
    '''
    Authentify session, using new CAS ticket
    See protocol on http://www.jasig.org/cas/protocol
    '''
    if force_login and force_password:
      # Login / Password from user form test
      login = force_login
      password = force_password
    else:
      # Decrypt password
      if not self.user.garmin_login and not force_login:
        raise Exception('Missing Garmin login')
      if not self.user.garmin_password and not force_password:
        raise Exception('Missing Garmin password')

      gpg = gnupg.GPG(gnupghome=self.GPG_HOME)
      login = self.user.garmin_login
      password = str(gpg.decrypt(self.user.garmin_password, passphrase=self.GPG_PASSPHRASE))
      if not password:
        raise Exception("No Garmin password available")

    self.session = requests.Session()

    # Get SSO server hostname
    res = self.session.get(self.url_hostname)
    sso_hostname = res.json().get('host', None)
    if not sso_hostname:
      raise GarminAuthException('No SSO server available')

    # Load login page to get login ticket
    params = {
      'clientId' : 'GarminConnect',
      'webhost' : sso_hostname,
    }
    res = self.session.get(self.url_login, params=params)
    if res.status_code != 200:
      raise GarminAuthException('No login form')

    # Get the login ticket value
    regex = '<input\s+type="hidden"\s+name="lt"\s+value="(?P<lt>\w+)"\s+/>'
    res = re.search(regex, res.text)
    if not res:
      raise GarminAuthException('No login ticket')
    login_ticket = res.group('lt')

    # Login/Password with login ticket
    # Send through POST
    data = {
      '_eventId' : 'submit', # Strange, but needed
      'lt' : login_ticket,
      'username' : login,
      'password' : password,
    }
    res = self.session.post(self.url_login, params=params, data=data)
    if res.status_code != 200:
      raise GarminAuthException('Authentification failed.')

    # Second auth step
    # Don't know why this one is necessary :/
    res = self.session.get(self.url_post_login)
    if res.status_code != 200:
      raise GarminAuthException('Second auth step failed.')

    # Check login
    res = self.session.get(self.url_check_login)
    garmin_user = res.json()
    if not garmin_user.get('username', None):
      raise GarminAuthException("Login check failed.")
    logger.info('Logged in as %s' % (garmin_user['username']))

    return garmin_user

  def check_tracks(self, page=0, nb_tracks=10):
    # Auth using stored login/pass
    if not self.session:
      self.auth()

    # Load paginated activities
    params = {
      'start' : page * nb_tracks,
      'limit' : nb_tracks,
    }
    resp = self.session.get(self.url_activity, params=params)
    data = resp.json()

    source = data['results'].get('activities', None)
    if source:
      source = [a['activity'] for a in source]

    return self.import_activities(source)

  def is_connected(self):
    return self.user.garmin_login and self.user.garmin_password

  def disconnect(self):
    # Just destroy credentials
    self.user.garmin_login = None
    self.user.garmin_password = None
    self.user.save()

  def get_activity_id(self, activity):
    return activity['activityId']

  def _load_extra_json(self, activity, data_type):
    # check in local cache
    f = self.get_file(activity, data_type)
    if f:
      return f

    # Load external json page
    activity_id = self.get_activity_id(activity)
    urls = {
      'laps'    : self.url_laps % activity_id,
      'details' : self.url_details % activity_id,
    }
    if data_type not in urls:
      raise Exception("Invalid data type %s" % data_type)

    resp = self.session.get(urls[data_type])
    if resp.encoding is None:
      resp.encoding = 'utf-8'

    # Store file locally
    self.store_file(activity, data_type, resp.content)

    return resp.content

  def build_line_coords(self, activity):
    '''
    Extract coords from Garmin measurements
    '''

    # First, load details
    details = self._load_extra_json(activity, 'details')
    details = json.loads(details)

    # Load metrics/measurements from file
    key = 'com.garmin.activity.details.json.ActivityDetails'
    if key not in details:
      raise Exception("Unsupported format")
    base = details[key]
    if 'measurements' not in base:
      raise Exception("Missing measurements")
    if 'metrics' not in base:
      raise Exception("Missing metrics")

    # Search latitude / longitude positions in measurements
    measurements = dict([(m['key'], m['metricsIndex']) for m in base['measurements']])
    if 'directLatitude' not in measurements or 'directLongitude' not in measurements:
      raise Exception("Missing lat/lon measurements")

    # Build linestring from metrics
    coords = []
    for m in base['metrics']:
      if 'metrics' not in m:
        continue
      lat, lng = m['metrics'][measurements['directLatitude']], m['metrics'][measurements['directLongitude']]
      if lat == 0.0 and lng == 0.0:
        continue
      coords.append((lat, lng))

    return coords

  def load_files(self, activity):
    # Load laps
    self._load_extra_json(activity, 'laps')

    # Load details
    self._load_extra_json(activity, 'details')

  def build_identity(self, activity):
    '''
    Extract Garmin data from raw activity
    '''
    identity = {}

    # Type of sport
    identity['sport'] = Sport.objects.get(slug=activity['activityType']['key'])
    logger.debug('Sport: %s' % identity['sport'])

    # Date
    t = int(activity['beginTimestamp']['millis']) / 1000
    identity['date'] = datetime.utcfromtimestamp(t).replace(tzinfo=utc)
    logger.debug('Date : %s' % identity['date'])

    # Time
    if False and 'sumMovingDuration' in activity:
      identity['time'] = timedelta(seconds=float(activity['sumMovingDuration']['value']))
    elif 'sumDuration' in activity:
      t = activity['sumDuration']['minutesSeconds'].split(':')
      identity['time'] = timedelta(minutes=float(t[0]), seconds=float(t[1]))
    else:
      raise Exception('No duration found.')
    logger.debug('Time : %s' % identity['time'])

    # Distance in km
    distance = activity.get('sumDistance')
    if distance:
      if distance['unitAbbr'] == 'm':
        identity['distance'] =  float(distance['value']) / 1000.0
      else:
        identity['distance'] =  float(distance['value'])
    else:
      identity['distance'] = 0.0
    logger.debug('Distance : %s km' % identity['distance'])

    # Speed
    identity['speed'] = time(0,0,0)
    if 'weightedMeanMovingSpeed' in activity:
      speed = activity['weightedMeanMovingSpeed']

      if speed['unitAbbr'] == 'km/h' or (speed['uom'] == 'kph' and identity['sport'].get_category() != 'running'):
        # Transform km/h in min/km
        s = float(speed['value'])
        if s != 0.0:
          mpk = 60.0 / s
          hour = int(mpk / 60.0)
          minutes = int(mpk % 60.0)
          seconds = int((mpk - minutes) * 60.0)
          identity['speed'] = time(hour, minutes, seconds)
      elif speed['unitAbbr'] == 'min/km':
        try:
          identity['speed'] = datetime.strptime(speed['display'], '%M:%S').time()
        except:
          s = float(speed['value'])
          if not math.isinf(s):
            minutes = int(s)
            identity['speed'] = time(0, minutes, int((s - minutes) * 60.0))
    logger.debug('Speed : %s' % identity['speed'])

    # update name
    skip_titles = ('Sans titre', 'No title', )
    name = activity['activityName']['value']
    identity['name'] = name not in skip_titles and name or ''

    return identity

  def build_splits(self, activity):
    # Load laps
    laps = self.get_file(activity, 'laps', format_json=True)
    if not laps or 'activity' not in laps or 'totalLaps' not in laps['activity']:
      return []
    laps = laps['activity']['totalLaps']['lapSummaryList']

    def _convert_speed(lap, name):
      # Convert a speed in m/s
      if name not in lap:
        return 0.0
      s = lap[name]
      if s['uom'] == 'kph':
        return float(s['value']) / 3.6
      if s['uom'] == 'hmph': # hetcometer per hour
        return float(s['value']) / 36
      return float(s['value'])

    def _convert_distance(lap, name):
      # Convert a distance in meters
      if name not in lap:
        return 0.0
      d = lap[name]
      if d['uom'] == 'kilometer':
        return float(d['value']) * 1000.0
      return float(d['value'])

    def _convert_date(lap, name):
      # Convert a timestamp to a datetime
      if name not in lap:
        return 0.0
      d = lap[name]
      tz = pytz.timezone(d['uom'])
      return make_aware(datetime.fromtimestamp(float(d['value']) / 1000.0), tz)

    def _convert_point(lap, name_lat, name_lng):
      # Build a point from lat,lng
      if name_lat not in lap or name_lng not in lap:
        return None
      return Point(float(lap[name_lat]['value']), float(lap[name_lng]['value']))

    def _convert_float(lap, name):
      # Convert to float a value
      if name not in lap:
        return 0.0
      return float(lap[name]['value'])

    # Build every split
    out = []
    for i, lap in enumerate(laps):
      split = TrackSplit(position=i+1)
      split.elevation_min = _convert_float(lap, 'MinElevation')
      split.elevation_max = _convert_float(lap, 'MaxElevation')
      split.elevation_gain = _convert_float(lap, 'GainElevation')
      split.elevation_loss = _convert_float(lap, 'LossElevation')
      split.speed_max = _convert_speed(lap, 'MaxSpeed')
      split.speed = _convert_speed(lap, 'WeightedMeanSpeed')
      split.distance = _convert_distance(lap, 'SumDistance')
      split.time = _convert_float(lap, 'SumDuration')
      split.energy = _convert_float(lap, 'SumEnergy')
      split.date_start = _convert_date(lap, 'BeginTimestamp')
      split.date_end = _convert_date(lap, 'EndTimestamp')
      split.position_start = _convert_point(lap, 'BeginLatitude', 'BeginLongitude')
      split.position_end = _convert_point(lap, 'EndLatitude', 'EndLongitude')
      out.append(split)

    return out

