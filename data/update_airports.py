# One-way sync to update airports table from OurAirports.
# Run with --live_run to actually execute DB changes.

# Install
# virtualenv env
# source env/bin/activate
# pip install unicodecsv mysql-connector mock

# To fetch data:
# $ curl -o ourairports.csv http://ourairports.com/data/airports.csv
import argparse
import codecs
import mysql.connector
import re
import sys
import unicodecsv

class OpenFlightsData(object):
  def __init__(self, iata=None, icao=None, countries=None):
    self.iata = iata or {}
    self.icao = icao or {}
    self.countries = countries or {}

  def load_all_airports(self, dbc):
    dbc.cursor.execute('SELECT * FROM airports')
    for row in dbc.cursor:
      self.iata[row['iata']] = row
      self.icao[row['icao']] = row

    dbc.cursor.execute('SELECT * FROM countries')
    for row in dbc.cursor:
      self.countries[row['oa_code']] = row['name']

  def find_by_iata(self, code):
    if code in self.iata:
      return self.iata[code]
    else:
      return None

  def find_by_icao(self, code):
    if code in self.icao:
      return self.icao[code]
    else:
      return None

  # Given a new OurAirports airport (oa), try to match it to existing data
  def match(self, dbc, oa):
    if oa['type'] == 'closed':
      return False

    # Does an airport with this ICAO entry already exist yet?
    of = self.find_by_icao(oa['ident'])
    if of:
      # Clean up random junk in IATA field (many '0' entries for some reason)
      if not re.match(r'[A-Z]{3}', oa['iata_code']):
        oa['iata_code'] = ''

      # Yes, but IATA code does not match
      if oa['iata_code'] != '' and of['iata'] != oa['iata_code']:
        print 'OLD %s (%s): IATA mismatch OF %s, OA %s' % (oa['ident'], oa['name'], of['iata'], oa['iata_code'])
        if of['iata'] != '':
          dupe = self.find_by_iata(oa['iata_code'])
          if dupe:
            # Existing entry has the same IATA, merge it with the new OA entry
            print '. MERGE IATA %s (%s) to new entry %s' % (dupe['iata'], dupe['name'], oa['ident'])
            dbc.move_iata_to_new_airport(oa['iata_code'], dupe['apid'], of['apid'])
      dbc.update_all_from_oa(of['apid'], oa)
      return True

    # New airport, but we only care about larger airports with ICAO identifiers
    if oa['type'] in ['medium_airport', 'large_airport'] and re.match(r'[A-Z]{4}', oa['ident']):
      oa['country_name'] = self.countries[oa['iso_country']]
      # Horrible hack for matching FAA LIDs
      if not oa['iata_code'] and len(oa['local_code']) == 3:
        oa['iata_code'] = oa['local_code']
      print 'NEW %s (%s): %s' % (oa['ident'], oa['name'], oa['iata_code'])
      if oa['iata_code'] == '':
        # No IATA or FAA LID, just create as new
        dbc.create_new_from_oa(oa)
      else:
        # Can we match by IATA/FAA?
        dupe = self.find_by_iata(oa['iata_code'])
        if dupe:
          print '. DUPE %s/%s (%s)' % (dupe['iata'], dupe['icao'], dupe['name'])
          # If not ICAO, or they're in the same country (first letter of ICAO), we assume ICAO code has changed 
          # and update existing entry with IATA using OA data (this preserves flights to it)
          if not dupe['icao'] or dupe['icao'][:1] == oa['ident'][:1]:
            print '.. ICAO match, update %s from %s to %s' % (dupe['iata'], dupe['icao'], oa['ident'])
            dbc.update_all_from_oa(dupe['apid'], oa)
          else:
            if oa['local_code'] == '':
              print '.. ICAO mismatch, deallocate IATA %s from %s and create %s/%s as new' % (
                dupe['iata'], dupe['icao'], oa['iata_code'], oa['ident'])
              dbc.dealloc_iata(dupe['apid'])
            else:
              print '.. IATA trumps FAA LID, ignore false duplicate'
              oa['iata_code'] = ''
            dbc.create_new_from_oa(oa)
        else:
          # No entry for this IATA, create as new
          dbc.create_new_from_oa(oa)
      return True
    return False

class DatabaseConnector(object):
  DB = 'flightdb2'

  def __init__(self, args=None):
    self.read_cnx = self.connect(host, pw)
    self.cursor = self.read_cnx.cursor(dictionary=True)
    self.write_cnx = self.connect(host, pw)
    self.write_cursor = self.write_cnx.cursor(dictionary=True)
    self.args = args

  def connect(self, host, pw):
    cnx = mysql.connector.connect(user='openflights', database=self.DB, host=host, password=pw)
    cnx.raise_on_warnings = True
    return cnx

  def safe_execute(self, sql, params):
    if args.live_run:
      self.write_cursor.execute(sql, params, )
      print ".. %s : %d rows updated" % (sql % params, self.write_cursor.rowcount)
      self.write_cnx.commit()
    else:
      print sql % params

  def update_all_from_oa(self, of_apid, oa):
    self.safe_execute(
      'UPDATE airports SET iata=%s, icao=%s, name=%s, x=%s, y=%s, elevation=%s, type=%s, source=%s WHERE apid=%s',
      ((oa['iata_code'] or None), oa['ident'], oa['name'], oa['longitude_deg'], oa['latitude_deg'], (oa['elevation_ft'] or 0),
        'airport', 'OurAirports', of_apid))

  def create_new_from_oa(self, oa):
    self.safe_execute(
      'INSERT INTO airports(name,city,country,iata,icao,x,y,elevation,type,source) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
      (oa['name'], oa['municipality'], oa['country_name'], (oa['iata_code'] or None), oa['ident'],
        oa['longitude_deg'], oa['latitude_deg'], (oa['elevation_ft'] or 0), 'airport', 'OurAirports'))

  def dealloc_iata(self, old_id):
    self.safe_execute('UPDATE airports SET iata=NULL WHERE apid=%s;', (old_id, ))

  def move_iata_to_new_airport(self, iata, old_id, new_id):
    self.dealloc_iata(old_id)
    self.safe_execute('UPDATE airports SET iata=%s WHERE apid=%s;', (iata, new_id, ))
    self.safe_execute('UPDATE flights SET src_apid=%s WHERE src_apid=%s;', (new_id, old_id, ))
    self.safe_execute('UPDATE flights SET dst_apid=%s WHERE dst_apid=%s;', (new_id, old_id, ))

if __name__ == "__main__":
  # Needed to allow piping UTF-8 (srsly Python wtf)
  sys.stdout = codecs.getwriter('utf8')(sys.stdout)

  parser = argparse.ArgumentParser()
  parser.add_argument('--live_run', default=False, action='store_true')
  parser.add_argument('--local', default=False, action='store_true')
  args = parser.parse_args()

  if args.local:
    host = 'localhost'
    pw = None
  else:
    host = '104.197.15.255'
    with open('../sql/db.pw','r') as f:
      pw = f.read().strip()

  dbc = DatabaseConnector(args)
  ofd = OpenFlightsData()
  ofd.load_all_airports(dbc)
  with open('ourairports.csv', 'rb') as csvfile:
    reader = unicodecsv.DictReader(csvfile, encoding='utf-8')
    for oa in reader:
      ofd.match(dbc, oa)
