# This module contains functions related to file handling.

import os
import time

from pUtil import tolog

def findLatestTRFLogFile(workdir):
    """ Find out which is the latest log.* file """

    last_log_file = ""

    # Assume the log files begin with 'log.'
    pattern = "log."
    file_list = sortedLs(workdir, pattern)
    if file_list != []:
        last_log_file = os.path.join(workdir, file_list[-1])
        tolog("Found payload log files: %s" % str(file_list))
        tolog("File %s was the last log file that was updated" % (last_log_file))
    else:
        tolog("Did not find any log.* files")

    return last_log_file

def sortedLs(path, pattern):
    """ Sort the contents of directory 'path' using 'pattern' """

    # Note: pattern is only a string, e.g. pattern = 'log.' will return a
    # list with all files starting with 'log.' in time order
    mtime = lambda f: os.stat(os.path.join(path, f)).st_mtime
    file_list = []
    try:
        file_list = list(sorted(os.listdir(path), key=mtime))
    except Exception, e:
        tolog("!!WARNING!!3232!! Failed to obtain sorted file list: %s" % (e))

    final_file_list = []
    if file_list != []:
        for f in file_list:
            if f.startswith(pattern):
                final_file_list.append(f)
    return final_file_list

def readFile(filename):
    """ Read the contents of a file """

    contents = ""
    if os.path.exists(filename):
        try:
            f = open(filename, 'r')
        except IOError, e:
            tolog("!!WARNING!!2121!! Failed to open file %s: %s" % (filename, e))
        else:
            try:
                contents = f.read()
            except Exception, e:
                tolog("!!WARNING!!2122!! Failed to read file %s: %s" % (filename, e))
            f.close()
    else:
        tolog("!!WARNING!!2121!! File does not exist: %s" % (filename))

    return contents

def writeFile(filename, contents):
    """ Write the contents to filename """

    status = False
    try:
        f = open(filename, 'w')
    except IOError, e:
        tolog("!!WARNING!!2123!! Failed to open file %s: %s" % (filename, e))
    else:
        try:
            f.write(contents)
        except IOError, e:
            tolog("!!WARNING!!2123!! Failed to write to file %s: %s" % (filename, e))
        else:
            status = True
        f.close()

    return status

# print findLatestTRFLogFile(os.getcwd())
