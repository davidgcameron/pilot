import commands
import os
import signal
import time
import re
import pUtil


def findProcessesInGroup(cpids, pid):
    """ recursively search for the children processes belonging to pid and return their pids
    here pid is the parent pid for all the children to be found
    cpids is a list that has to be initialized before calling this function and it contains
    the pids of the children AND the parent as well """

    cpids.append(pid)
    psout = commands.getoutput("ps -eo pid,ppid -m | grep %d" % pid)
    lines = psout.split("\n")
    if lines != ['']:
        for i in range(0, len(lines)):
            thispid = int(lines[i].split()[0])
            thisppid = int(lines[i].split()[1])
            if thisppid == pid:
                findProcessesInGroup(cpids, thispid)

def isZombie(pid):
    """ Return True if pid is a zombie process """

    zombie = False

    out = commands.getoutput("ps aux | grep %d" % (pid))
    if "<defunct>" in out:
        zombie = True

    return zombie

def getProcessCommands(euid, pids):
    """ return a list of process commands corresponding to a pid list for user euid """

    _cmd = 'ps u -u %d' % (euid)
    processCommands = []
    ec, rs = commands.getstatusoutput(_cmd)
    if ec != 0:
        pUtil.tolog("Command failed: %s" % (rs))
    else:
        # extract the relevant processes
        pCommands = rs.split('\n') 
        first = True
        for pCmd in pCommands:
            if first:
                # get the header info line
                processCommands.append(pCmd)
                first = False
            else:
                # remove extra spaces
                _pCmd = pCmd
                while "  " in _pCmd:
                    _pCmd = _pCmd.replace("  ", " ")
                items = _pCmd.split(" ")
                for pid in pids:
                    # items = username pid ...
                    if items[1] == str(pid):
                        processCommands.append(pCmd)
                        break

    return processCommands


def printProcessTree():
    import subprocess
    pl = subprocess.Popen(['ps', '--forest', '-ef'], stdout=subprocess.PIPE).communicate()[0]
    pUtil.tolog(pl)

def dumpStackTrace(pid):
    """ run the stack trace command """

    # make sure that the process is not in a zombie state
    if not isZombie(pid):
        pUtil.tolog("Running stack trace command on pid=%d:" % (pid))
        cmd = "pstack %d" % (pid)
        out = commands.getoutput(cmd)
        if out == "":
            pUtil.tolog("(pstack returned empty string)")
        else:
            pUtil.tolog(out)
    else:
        pUtil.tolog("Skipping pstack dump for zombie process")

def killProcesses(pid):
    """ kill a job upon request """

    #printProcessTree()
    # firstly find all the children process IDs to be killed
    kids = []
    findProcessesInGroup(kids, pid)
    # reverse the process order so that the athena process is killed first 
    #(otherwise the stdout will be truncated)
    kids.reverse()
    pUtil.tolog("Process IDs to be killed: %s (in reverse order)" % str(kids))

    # find which commands are still running
    try:
        cmds = getProcessCommands(os.geteuid(), kids)
    except Exception, e:
        pUtil.tolog("getProcessCommands() threw an exception: %s" % str(e))
    else:
        if len(cmds) <= 1:
            pUtil.tolog("Found no corresponding commands to process id(s)")
        else:
            pUtil.tolog("Found commands still running:")
            for cmd in cmds:
                pUtil.tolog(cmd)

            # loop over all child processes
            first = True
            for i in kids:
                # dump the stack trace before killing it
                dumpStackTrace(i)

                # kill the process gracefully
                try:
                    os.kill(i, signal.SIGTERM)
                except Exception,e:
                    pUtil.tolog("WARNING: Exception thrown when killing the child process %d under SIGTERM, wait for kill -9 later: %s" % (i, str(e)))
                    pass
                else:
                    pUtil.tolog("Killed pid: %d (SIGTERM)" % (i))

                if first:
                    _t = 60
                    first = False
                else:
                    _t = 10
                pUtil.tolog("Sleeping %d s to allow process to exit" % (_t))
                time.sleep(_t)
    
                # now do a hardkill just in case some processes haven't gone away
                try:
                    os.kill(i, signal.SIGKILL)
                except Exception,e:
                    pUtil.tolog("WARNING: Exception thrown when killing the child process %d under SIGKILL, ignore this if it is already killed by previous SIGTERM: %s" % (i, str(e)))
                    pass
                else:
                    pUtil.tolog("Killed pid: %d (SIGKILL)" % (i))

def checkProcesses(pid):
    """ Check the number of running processes """

    kids = []
    n = 0
    try:
        findProcessesInGroup(kids, pid)
    except Exception, e:
        pUtil.tolog("!!WARNING!!2888!! Caught exception in findProcessesInGroup: %s" % (e))
    else:
        n = len(kids)
        pUtil.tolog("Number of running processes: %d" % (n))
    return n

def killOrphans():
    """ Find and kill all orphan processes belonging to current pilot user """

    pUtil.tolog("Searching for orphan processes")
    cmd = "ps -o pid,ppid,comm -u %s" % (commands.getoutput("whoami"))
    processes = commands.getoutput(cmd)
    pattern = re.compile('(\d+)\s+(\d+)\s+(\S+)')

    count = 0
    for line in processes.split('\n'):
        ids = pattern.search(line)
        if ids:
            pid = ids.group(1)
            ppid = ids.group(2)
            comm = ids.group(3)
            if ppid == '1':
                count += 1
                pUtil.tolog("Found orphan process: pid=%s, ppid=%s" % (pid, ppid))
                cmd = 'kill -9 %s' % (pid)
                ec, rs = commands.getstatusoutput(cmd)
                if ec != 0:
                    pUtil.tolog("!!WARNING!!2999!! %s" % (rs))
                else:
                    pUtil.tolog("Killed orphaned process %s (%s)" % (pid, comm))

    if count == 0:
        pUtil.tolog("Did not find any orphan processes")
    else:
        pUtil.tolog("Found %d orphan process(es)" % (count))
