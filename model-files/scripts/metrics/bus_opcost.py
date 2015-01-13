import logging
import os
import re
import shutil
import subprocess
import tempfile
import sys

import numpy
import pandas
import shapefile # for reading dbfs https://github.com/GeospatialPython/pyshp
import xlrd

USAGE = """
 python bus_opcost.py

 Reads trn/trnlink[am|md|pm|ev|ea]_wlk_com_wlk.dbf

 - Joins with the hwy network for city-based bus operating costs per link
 - Assumes freeway opcost based on county for those that have no specification
 - Sums it up by county
 
 Outputs a sum of ALL bus VMT and opcosts by county into 

   metrics\bus_opcost.csv

 """


TRNLINE_HEADER = ["name","mode","owner","frequency","line time","line dist","total boardings","passenger miles","passenger hours","path id"]

EFFECTIVE_TRN_HOURS = {'timeperiod': ['ea','am','md','pm','ev'],
                       'effective hours':[2,4,5,4,4]}

BUS_OPCOST_FILE  = os.path.join("INPUT","params.properties")

ROADWAY_NETWORK  = os.path.join("hwy","avgloadAM.net")

OUTFILE          = os.path.join("metrics", "bus_opcost.csv")

GL_TO_COUNTY = {
  1 :'San Francisco',
  2 :'San Mateo',
  3 :'Santa Clara',
  4 :'Alameda',
  5 :'Contra Costa',
  6 :'Solano',
  7 :'Napa',
  8 :'Sonoma',
  9 :'Marin',
  10:'Periphery'
}

CUBE_ROADWAY_ATTRIBUTES = ["A","B","GL","CITYID","CITYNAME","BUSOPC_PERFECT","BUSOPC_PAVE","BUS_RM_ADJUST","BUS_FU_ADJUST","BUSOPC"]
CUBE_EXPORT_SCRIPT_NAME = "cube_export.s"
CUBE_EXPORT_SCRIPT = r"""
; script generated by bus_opcost.py
RUN PGM=NETWORK
 FILEI NETI[1]="%s"
 FILEO LINKO=%s,FORMAT=SDF,INCLUDE=%s
ENDRUN
"""

def read_pavement_costs():
    """
    Read the pavement cost config file.
    Returns a dictionary with variables.
    """
    IGNORE_re   = r"^((\s*)|(\s*[;#].*))$"  # whitespace or comment (with optional preceding whitespace)
    VAR_re      = r"^(\S+)\s*=\s*(\S+)"  # X = Y
    returndict  = {}
    f = open(BUS_OPCOST_FILE,'r')
    for line in f:
        line = line.strip()
        if re.match(IGNORE_re, line):
            # print "Ignoring [%s]" % line
            continue
        m = re.match(VAR_re, line)
        if m == None:
            raise Exception("Don't understand line [%s] in %s" % (line, BUS_OPCOST_FILE))

        returndict[m.group(1)] = float(m.group(2))
    f.close()
    return returndict

def runCubeScript(tempdir, script_filename):
    """
    Run the cube script specified in the tempdir specified.
    Returns the return code.
    """
    # run it
    proc = subprocess.Popen("runtpp %s" % script_filename, cwd=tempdir, 
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    for line in proc.stdout:
        line = line.strip('\r\n')
        logger.info("  stdout: " + line)
    for line in proc.stderr:
        line = line.strip('\r\n')
        logger.info("  stderr: " + line)
    retcode = proc.wait()
    if retcode == 2:
        raise Exception("Failed to run Cube script %s" % (script_filename))
    logger.info("Received %d from 'runtpp %s'" % (retcode, script_filename))

def readCubeNetwork():
    """
    Reads the Cube roadway network specified by the given filename.
    
    Returns a pandas dataframe with the attributes of the links.
    """
    # get the tail of the filename to use for the intermediate files
    filename = os.path.abspath(ROADWAY_NETWORK)
    (head,tail) = os.path.split(filename)
    # strip the suffix
    if tail.find(".") >= 0:
        tail = tail[:tail.find(".")]
    links_filename = "%s_links.csv" % tail
    
    tempdir = tempfile.mkdtemp()
    script_filename = os.path.join(tempdir, CUBE_EXPORT_SCRIPT_NAME)    

    # write the script file
    script_file = open(script_filename, "w")
    script_file.write(CUBE_EXPORT_SCRIPT % (filename, links_filename, ",".join(CUBE_ROADWAY_ATTRIBUTES)))
    script_file.close()
    logger.info("Wrote %s" % script_filename)

    runCubeScript(tempdir, script_filename)
        
    # read the link csv
    links = pandas.read_table(os.path.join(tempdir, links_filename), sep=",",
                              header=None, names=CUBE_ROADWAY_ATTRIBUTES)
    # links.set_index(['A','B'],inplace=True)

    logger.info("Read %d links from %s" % (len(links), links_filename))
    
    # clean up tempdir
    logger.info("Deleting %s" % tempdir)
    shutil.rmtree(tempdir)
    
    return links

if __name__ == '__main__':
    logger = logging.getLogger('bus_opcost')
    consolehandler = logging.StreamHandler()
    consolehandler.setLevel(logging.DEBUG)
    consolehandler.setFormatter(logging.Formatter('%(asctime)-15s %(name)-12s: %(levelname)-8s %(message)s', datefmt='%d %b %Y %H:%M:%S'))
    logger.addHandler(consolehandler)
    logger.setLevel(logging.DEBUG)

    # collect the data in here
    full_table_df = None
    set_fulltable = False

    for timeperiod in ['ea','am','md','pm','ev']:
        # we only need to read wlk_com_wlk because every line is there
        filename = os.path.join("trn", "trnlink%s_wlk_com_wlk.dbf" % timeperiod)
        mydbf = open(filename, "rb")
        sfr = shapefile.Reader(shp=None, dbf=mydbf)
        colnames = []
        for field in sfr.fields[1:]:
            colnames.append(field[0])
        temp_df = pandas.DataFrame(sfr.records(), columns=colnames)
        # remove support modes http://analytics.mtc.ca.gov/foswiki/Main/TransitModes
        temp_df = temp_df[temp_df.MODE >=10]
        # remove non-roadway nodes (rail, ferry)
        temp_df = temp_df[temp_df.MODE <100]
        # remove zero time/distance
        temp_df = temp_df[(temp_df.TIME > 0) & (temp_df.DIST > 0)]
        # add timeperiod
        temp_df['timeperiod'] = timeperiod

        if set_fulltable==False: # it doesn't like checking if a dataFrame is none
            full_table_df = temp_df
            set_fulltable = True
        else:
            full_table_df = full_table_df.append(temp_df)

        logger.info("Read %6d links from %s; total links = %6d" % (len(temp_df), filename, len(full_table_df)))

    # effective hours for that timeperiod
    full_table_df = pandas.merge(full_table_df, pandas.DataFrame(EFFECTIVE_TRN_HOURS), how='left')

    # set up operator id
    full_table_df['operator'] = full_table_df['NAME'].str.extract('^(\d+)_')
    full_table_df['operator'] = full_table_df['operator'].apply(int)    

    # read network
    roadway_links = readCubeNetwork()
    # roadway attributes
    full_table_df = pandas.merge(full_table_df,roadway_links, how='left')
    

    # read opcost config
    pvcosts = read_pavement_costs()
    # these have no BUSOPC
    # print full_table_df.MODE[full_table_df['BUSOPC'].isnull()].value_counts()
    # set them to default
    full_table_df.BUSOPC[full_table_df['BUSOPC'].isnull()] = \
      (pvcosts['BusOpCost_perfect_RM'  ]*pvcosts['BusOpCost_fwyadj_RM'  ]) + \
      (pvcosts['BusOpCost_perfect_Fuel']*pvcosts['BusOpCost_fwyadj_Fuel'])

    # aggregate for each route
    byroute = full_table_df.groupby(['NAME','timeperiod'])
    byroute_agg = byroute.agg({'DIST':numpy.sum,
                               'TIME':numpy.sum,
                               'FREQ':numpy.mean,
                               'effective hours':numpy.mean})

    # these are in hundredths of miles and hundredths of minutes, respectively
    # convert distance and time to miles and minutes
    byroute_agg['route DIST'] = byroute_agg['DIST']*0.01
    byroute_agg['route TIME'] = byroute_agg['TIME']*0.01

    # route speed (miles per hour)
    byroute_agg['route speed'] = 60.0*byroute_agg['route DIST']/byroute_agg['route TIME']
    # route vehicles. Can't have a fraction of a vehicle.
    byroute_agg['route vehicles'] = numpy.ceil(byroute_agg['route TIME']/byroute_agg['FREQ'])
    # print byroute_agg
    byroute_agg = byroute_agg.reset_index()

    # put the route vehicles back with the links
    full_table_df = full_table_df.merge(byroute_agg[['NAME','timeperiod','route speed','route vehicles','route DIST','route TIME']], 
                                        how='left')

    # link VMT per hour.  
    # Assuming that the route vehicles are always moving around for the whole hour, apportion some to this link based on distance
    full_table_df['link VMT per hour'] = full_table_df['route vehicles']*full_table_df['route speed']*(0.01*full_table_df['DIST']/full_table_df['route DIST'])
    # link VMT = link VMT per hour * effective hours per time period
    full_table_df['VMT'] = full_table_df['link VMT per hour']*full_table_df['effective hours']

    # link opcost for the period - convert from year 2000 cents to year 2000 dollars
    full_table_df['opcost'] = full_table_df['VMT']*full_table_df['BUSOPC']*0.01

    # output summed to operator -- for debugging
    # byoperator      = full_table_df.groupby(['operator'])
    # byoperator_agg  = byoperator.agg({'VMT'   :numpy.sum,
    #                                   'opcost':numpy.sum})
    # byoperator_agg.to_csv(os.path.join("metrics","busopcost_operator.csv"))

    # output summed to county
    bycounty     = full_table_df.groupby(['GL'])
    bycounty_agg = bycounty.agg({'VMT'   :numpy.sum,
                                 'opcost'     :numpy.sum})
    bycounty_agg = bycounty_agg.reset_index()

    gl_to_county_df = pandas.DataFrame.from_dict(GL_TO_COUNTY, orient='index')
    gl_to_county_df.index.name = 'GL'
    gl_to_county_df = gl_to_county_df.rename(columns={0:'county'})
    gl_to_county_df = gl_to_county_df.reset_index()

    bycounty_agg = bycounty_agg.merge(gl_to_county_df, how='left')

    bycounty_agg.to_csv(OUTFILE, index=False)
    print "Wrote %s" % OUTFILE
