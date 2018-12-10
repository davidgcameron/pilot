# Class definition:
#   RunJobHpcEvent
#   This class is the base class for the HPC Event Server classes.
#   Instances are generated with RunJobFactory via pUtil::getRunJob()
#   Implemented as a singleton class
#   http://stackoverflow.com/questions/42558/python-and-the-singleton-pattern

import commands
import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback

# Import relevant python/pilot modules
# Pilot modules
import Job
import Node
import Site
import pUtil
import RunJobUtilities
import Mover as mover

from ThreadPool import ThreadPool
from RunJob import RunJob              # Parent RunJob class
from JobState import JobState
from JobRecovery import JobRecovery
from PilotErrors import PilotErrors
from ErrorDiagnosis import ErrorDiagnosis
from pUtil import tolog, getExperiment, isAnalysisJob, createPoolFileCatalog, getSiteInformation, getDatasetDict
from objectstoreSiteMover import objectstoreSiteMover
from Mover import getFilePathForObjectStore, getInitialTracingReport
from PandaServerClient import PandaServerClient
import EventRanges

from GetJob import GetJob
from HPC.HPCManager import HPCManager

class RunJobHpcEvent(RunJob):

    # private data members
    __runjob = "RunJobHpcEvent"                            # String defining the sub class
    __instance = None                           # Boolean used by subclasses to become a Singleton
    #__error = PilotErrors()                     # PilotErrors object

    # Required methods

    def __init__(self):
        """ Default initialization """

        # e.g. self.__errorLabel = errorLabel
        pass
        self.__output_es_files = []
        self.__eventRanges = {}
        self.__failedStageOuts = []
        self.__hpcManager = None
        self.__stageout_threads = 1
        self.__userid = None

        self.__stageinretry = 1
        self.__siteInfo = None
        # multi-jobs
        self.__firstJob = True
        self.__firstJobId = None
        self.__pilotWorkingDir = None
        self.__jobs = {}
        self.__jobEventRanges = {}
        self.__nJobs = 1
        self.__hpcMode = 'normal'
        self.__hpcStatue = 'starting'
        self.__hpcCoreCount = 0
        self.__hpcEventRanges = 0
        self.__hpcJobId = None
        self.__neededEventRanges = 0
        self.__maxEventsPerJob = 1000
        self.__neededJobs = None
        self.__avail_files = {}
        self.__avail_tag_files = {}

        # event Stager
        self.__yoda_to_os = False
        self.__yoda_to_zip = False
        self.__es_to_zip = False
        self.__stageout_status = False

        # for recovery
        self.__jobStateFile = None


    def __new__(cls, *args, **kwargs):
        """ Override the __new__ method to make the class a singleton """

        if not cls.__instance:
            cls.__instance = super(RunJobHpcEvent, cls).__new__(cls, *args, **kwargs)

        return cls.__instance

    def getRunJob(self):
        """ Return a string with the experiment name """

        return self.__runjob

    def getRunJobFileName(self):
        """ Return the filename of the module """

        return super(RunJobHpcEvent, self).getRunJobFileName()

    # def argumentParser(self):  <-- see example in RunJob.py

    def allowLoopingJobKiller(self):
        """ Should the pilot search for looping jobs? """

        # The pilot has the ability to monitor the payload work directory. If there are no updated files within a certain
        # time limit, the pilot will consider the as stuck (looping) and will kill it. The looping time limits are set
        # in environment.py (see e.g. loopingLimitDefaultProd)

        return False
        
    def setupHPCEvent(self, rank=None):
        self.__jobSite = Site.Site()
        self.__jobSite.setSiteInfo(self.argumentParser())
        self.__logguid = None
        ## For HPC job, we don't need to reassign the workdir
        # reassign workdir for this job
        self.__jobSite.workdir = self.__jobSite.wntmpdir
        if not os.path.exists(self.__jobSite.workdir):
            os.makedirs(self.__jobSite.workdir)


        tolog("runJobHPCEvent.getPilotLogFilename=%s"% self.getPilotLogFilename())
        if self.getPilotLogFilename() != "":
            pilotLogFilename = self.getPilotLogFilename()
            if rank:
                pilotLogFilename = '%s.%s' % (pilotLogFilename, rank)
            tolog("runJobHPCEvent.setPilotLogFilename=%s"% pilotLogFilename)
            pUtil.setPilotlogFilename(pilotLogFilename)

        # set node info
        self.__node = Node.Node()
        self.__node.setNodeName(os.uname()[1])
        self.__node.collectWNInfo(self.__jobSite.workdir)

        # redirect stderr
        #sys.stderr = open("%s/runJobHPCEvent.stderr" % (self.__jobSite.workdir), "w")

        self.__pilotWorkingDir = self.getParentWorkDir()
        tolog("Pilot workdir is: %s" % self.__pilotWorkingDir)
        os.chdir(self.__pilotWorkingDir)
        tolog("Current job workdir is: %s" % os.getcwd())
        # self.__jobSite.workdir = self.__pilotWorkingDir
        tolog("Site workdir is: %s" % self.__jobSite.workdir)

        # get the experiment object
        self.__thisExperiment = getExperiment(self.getExperiment())
        tolog("runEvent will serve experiment: %s" % (self.__thisExperiment.getExperiment()))
        self.__siteInfo = getSiteInformation(self.getExperiment())

    def getDefaultResources(self):
        siteInfo = self.__siteInfo
        catchalls = siteInfo.readpar("catchall")
        values = {}
        res = {}
        if "yoda_to_os" in catchalls:
            res['yoda_to_os'] = True
        else:
            res['yoda_to_os'] = False
        self.__yoda_to_os = res['yoda_to_os']

        if "es_to_zip" in catchalls:
            res['es_to_zip'] = True
        else:
            res['es_to_zip'] = False
        self.__es_to_zip = res['es_to_zip']

        if "yoda_to_zip" in catchalls:
            res['yoda_to_zip'] = True
        else:
            res['yoda_to_zip'] = False
        self.__yoda_to_zip = res['yoda_to_zip']

        if "copyOutputToGlobal" in catchalls:
            res['copyOutputToGlobal'] = True
        else:
            res['copyOutputToGlobal'] = False

        for catchall in catchalls.split(","):
            if '=' in catchall:
                values[catchall.split('=')[0]] = catchall.split('=')[1]

        res['queue'] = values.get('queue', 'regular')
        res['mppwidth'] = values.get('mppwidth', 48)
        res['mppnppn'] = values.get('mppnppn', 1)
        res['walltime_m'] = values.get('walltime_m', 30)
        res['ATHENA_PROC_NUMBER'] = values.get('ATHENA_PROC_NUMBER', 23)
        res['max_nodes'] = values.get('max_nodes', 3)
        res['min_walltime_m'] = values.get('min_walltime_m', 20)
        res['max_walltime_m'] = values.get('max_walltime_m', 2000)
        res['nodes'] = values.get('nodes', 2)
        if self.getYodaNodes():
            res['nodes'] = self.getYodaNodes()
        if self.getYodaQueue():
            res['queue'] = self.getYodaQueue()
        res['min_nodes'] = values.get('min_nodes', 1)
        res['cpu_per_node'] = values.get('cpu_per_node', 24)
        res['partition'] = values.get('partition', None)
        res['repo'] = values.get('repo', None)
        res['max_events'] = values.get('max_events', 10000)
        res['initialtime_m'] = values.get('initialtime_m', 15)
        res['time_per_event_m'] = values.get('time_per_event_m', 10)
        res['mode'] = values.get('mode', 'normal')
        res['backfill_queue'] = values.get('backfill_queue', 'regular')
        res['stageout_threads'] = int(values.get('stageout_threads', 4))
        res['copy_input_files'] = values.get('copy_input_files', 'false').lower()
        res['plugin'] =  values.get('plugin', 'pbs').lower()
        res['localWorkingDir'] =  values.get('localWorkingDir', None)
        res['parallel_jobs'] = values.get('parallel_jobs', 1)
        res['events_limit_per_job'] = int(values.get('events_limit_per_job', 1000))

        if 'debug' in res['queue']:
            res['walltime_m'] = 30

        siteInfo = getSiteInformation(self.getExperiment())
        # get the copy tool
        setup = siteInfo.getCopySetup(stageIn=False)
        tolog("Copy Setup: %s" % (setup))
        # espath = getFilePathForObjectStore(filetype="eventservice")
        ddmendpoint = siteInfo.getObjectstoreDDMEndpoint(os_bucket_name='eventservice')
        os_bucket_id = siteInfo.getObjectstoreBucketID(ddmendpoint)
        tolog("Will use the default bucket ID: %s" % (os_bucket_id))
        espath = siteInfo.getObjectstorePath(os_bucket_id=os_bucket_id, label='w')
        tolog("ES path: %s" % (espath))
        os_bucket_id = siteInfo.getObjectstoresField('os_bucket_id', 'eventservice')
        tolog("The default bucket ID: %s for queue %s" % (os_bucket_id, self.__jobSite.computingElement))

        res['setup'] = setup
        res['esPath'] = espath
        res['os_bucket_id'] = os_bucket_id

        return res

    def getYodaSetup(self):
        siteInfo = self.__siteInfo
        envsetup = siteInfo.readpar("envsetup")
        setupPath = os.path.dirname(envsetup)
        yodaSetup = os.path.join(setupPath, 'yodasetup.sh')
        if os.path.exists(yodaSetup):
            setup = ""
            f = open(yodaSetup)
            for line in f:
                setup += line + "\n"
            f.close()
            return setup
        return None

    def setupHPCManager(self):
        logFileName = None
        tolog("runJobHPCEvent.getPilotLogFilename=%s"% self.getPilotLogFilename())
        if self.getPilotLogFilename() != "":
            logFileName = self.getPilotLogFilename()

        defRes = self.getDefaultResources()

        if defRes['copy_input_files'] == 'true' and defRes['localWorkingDir']:
            self.__copyInputFiles = True
        else:
            self.__copyInputFiles = False
        self.__nJobs = defRes['parallel_jobs']
        self.__stageout_threads = defRes['stageout_threads']
        self.__copyOutputToGlobal = defRes['copyOutputToGlobal']

        tolog("Setup HPC Manager")
        hpcManager = HPCManager(globalWorkingDir=self.__pilotWorkingDir, localWorkingDir=defRes['localWorkingDir'], logFileName=logFileName, copyInputFiles=self.__copyInputFiles)

        #jobStateFile = '%s/jobState-%s.pickle' % (self.__pilotWorkingDir, self.__job.jobId)
        #hpcManager.setPandaJobStateFile(jobStateFile)
        self.__hpcMode = "HPC_" + hpcManager.getMode(defRes)
        self.__hpcStatue = 'waitingResource'
        pluginName = defRes.get('plugin', 'pbs')
        hpcManager.setupPlugin(pluginName)

        tolog("Get Yoda setup")
        yodaSetup = self.getYodaSetup()
        tolog("Yoda setup: %s" % yodaSetup)
        hpcManager.setLocalSetup(yodaSetup)

        tolog("HPC Manager getting free resouces")
        hpcManager.getFreeResources(defRes)
        self.__hpcStatue = 'gettingJobs'

        tolog("HPC Manager getting needed events number")
        self.__hpcEventRanges = hpcManager.getEventsNumber()
        tolog("HPC Manager needs events: %s, max_events: %s; use the smallest one." % (self.__hpcEventRanges, defRes['max_events']))
        if self.__hpcEventRanges > int(defRes['max_events']):
            self.__hpcEventRanges = int(defRes['max_events'])
        self.__neededEventRanges = self.__hpcEventRanges
        self.__maxEventsPerJob = defRes['events_limit_per_job']

        self.__hpcManager = hpcManager
        tolog("HPC Manager setup finished")

    def setupJob(self, job, data):
        tolog("setupJob")
        try:
            job.coreCount = 0
            job.hpcEvent = True
            if self.__firstJob:
                job.workdir = self.__jobSite.workdir
                self.__firstJob = False
            else:
                # job.mkJobWorkdir(self.__pilotWorkingDir)
                pass
            job.experiment = self.getExperiment()
            # figure out and set payload file names
            job.setPayloadName(self.__thisExperiment.getPayloadName(job))
            # reset the default job output file list which is anyway not correct
            job.outFiles = []
        except Exception, e:
            pilotErrorDiag = "Failed to process job info: %s" % str(e)
            tolog("!!WARNING!!3000!! %s" % (pilotErrorDiag))
            
            self.failOneJob(0, PilotErrors.ERR_UNKNOWN, job, pilotErrorDiag=pilotErrorDiag, final=True, updatePanda=False)
            return -1

        current_dir = self.__pilotWorkingDir
        os.chdir(job.workdir)

        tolog("Switch from current dir %s to job %s workdir %s" % (current_dir, job.jobId, job.workdir))

        self.__userid = job.prodUserID
        self.__jobs[job.jobId] = {'job': job}
        # prepare for the output file data directory
        # (will only created for jobs that end up in a 'holding' state)
        job.datadir = self.__pilotWorkingDir + "/PandaJob_%s_data" % (job.jobId)

        # See if it's an analysis job or not
        trf = job.trf
        self.__jobs[job.jobId]['analysisJob'] = isAnalysisJob(trf.split(",")[0])

        # Setup starts here ................................................................................

        # Update the job state file
        job.jobState = "starting"
        job.setHpcStatus('init')


        # Send [especially] the process group back to the pilot
        job.setState([job.jobState, 0, 0])
        job.jobState = job.result
        rt = RunJobUtilities.updatePilotServer(job, self.getPilotServer(), self.getPilotPort())

        JR = JobRecovery(pshttpurl='https://pandaserver.cern.ch', pilot_initdir=job.workdir)
        JR.updateJobStateTest(job, self.__jobSite, self.__node, mode="test")
        JR.updatePandaServer(job, self.__jobSite, self.__node, 25443)
        self.__jobs[job.jobId]['job'] = job
        self.__jobs[job.jobId]['JR'] = JR

        # prepare the setup and get the run command list
        ec, runCommandList, job, multi_trf = self.setup(job, self.__jobSite, self.__thisExperiment)
        if ec != 0:
            tolog("!!WARNING!!2999!! runJob setup failed: %s" % (job.pilotErrorDiag))
            self.failOneJob(0, ec, job, pilotErrorDiag=job.pilotErrorDiag, final=True, updatePanda=False)
            return -1
        tolog("Setup has finished successfully")


        # job has been updated, display it again
        job.displayJob()
        tolog("RunCommandList: %s" % runCommandList)
        tolog("Multi_trf: %s" % multi_trf)
        self.__jobs[job.jobId]['job'] = job
        self.__jobs[job.jobId]['JR'] = JR
        self.__jobs[job.jobId]['runCommandList'] = runCommandList
        self.__jobs[job.jobId]['multi_trf'] = multi_trf

        # backup job file
        filename = os.path.join(self.__pilotWorkingDir, "Job_%s.json" % job.jobId)
        content = {'workdir': job.workdir, 'data': data, 'experiment': self.getExperiment(), 'runCommandList': runCommandList}
        with open(filename, 'w') as outputFile:
            json.dump(content, outputFile)

        # copy queue data
        try:
            copy_src = self.__siteInfo.getQueuedataFileName()
            copy_dest = os.path.join(job.workdir, os.path.basename(copy_src))
            tolog("Copy %s to %s" % (copy_src, copy_dest))
            shutil.copyfile(copy_src, copy_dest)
        except:
            tolog("Failed to copy queuedata to job working dir: %s" % (traceback.format_exc()))

        tolog("Switch back from job %s workdir %s to current dir %s" % (job.jobId, job.workdir, current_dir))
        os.chdir(current_dir)
        return 0


    def getHPCEventJobFromPanda(self, nJobs=1):
        try:
            tolog("Switch to pilot working dir: %s" % self.__pilotWorkingDir)
            os.chdir(self.__pilotWorkingDir)
            tolog("Get new job from Panda")
            getJob = GetJob(self.__pilotWorkingDir, self.__node, self.__siteInfo, self.__jobSite)
            jobs, data, errLog = getJob.getNewJob(nJobs=nJobs)
            if not jobs:
                if "No job received from jobDispatcher" in errLog or "Dispatcher has no jobs" in errLog:
                    errorText = "!!FINISHED!!0!!Dispatcher has no jobs"
                else:
                    errorText = "!!FAILED!!1999!!%s" % (errLog)
                tolog(errorText)

                # remove the site workdir before exiting
                # pUtil.writeExitCode(thisSite.workdir, error.ERR_GENERALERROR)
                # raise SystemError(1111)
                #pUtil.fastCleanup(self.__jobSite.workdir, self.__pilotWorkingDir, True)
                return -1
            else:
                for job in jobs:
                    tolog("download job definition id: %s" % (job.jobDefinitionID))
                    # verify any contradicting job definition parameters here
                    try:
                        ec, pilotErrorDiag = self.__thisExperiment.postGetJobActions(job)
                        if ec == 0:
                            tolog("postGetJobActions: OK")
                            # return ec
                        else:
                            tolog("!!WARNING!!1231!! Post getJob() actions encountered a problem - job will fail")
                            try:
                                # job must be failed correctly
                                pUtil.tolog("Updating PanDA server for the failed job (error code %d)" % (ec))
                                job.jobState = 'failed'
                                job.setState([job.jobState, 0, ec])
                                # note: job.workdir has not been created yet so cannot create log file
                                pilotErrorDiag = "Post getjob actions failed - workdir does not exist, cannot create job log, see batch log"
                                tolog("!!WARNING!!2233!! Work dir has not been created yet so cannot create job log in this case - refer to batch log")

                                JR = JobRecovery(pshttpurl='https://pandaserver.cern.ch', pilot_initdir=job.workdir)
                                JR.updatePandaServer(job, self.__jobSite, self.__node, 25443) 
                                #pUtil.fastCleanup(self.__jobSite.workdir, self.__pilotWorkingDir, True)
                                #return ec
                                continue
                            except Exception, e:
                                pUtil.tolog("Caught exception: %s" % (e))
                                #return ec
                                continue
                    except Exception, e:
                        pUtil.tolog("Caught exception: %s" % (e))
                        #return -1
                        continue
                    self.setupJob(job, data[job.jobId])

                    tolog("Get Event Ranges for job %s" % job.jobId)
                    eventRanges = self.getJobEventRanges(job, numRanges=self.__neededEventRanges)
                    self.__neededEventRanges = self.__neededEventRanges - len(eventRanges)
                    tolog("Get %s Event ranges for job %s" % (len(eventRanges), job.jobId))
                    if len(eventRanges) > self.__maxEventsPerJob:
                        self.__maxEventsPerJob = len(eventRanges)
                    self.__eventRanges[job.jobId] = {}
                    self.__jobEventRanges[job.jobId] = eventRanges
                    for eventRange in eventRanges:
                        self.__eventRanges[job.jobId][eventRange['eventRangeID']] = 'new'

                    self.updateJobState(job, 'starting', '', final=False)
        except:
            tolog("Failed to get job: %s" % (traceback.format_exc()))
            return -1

        return 0

    def getHPCEventJobFromEnv(self):
        tolog("getHPCEventJobFromEnv")
        try:
            # always use this filename as the new jobDef module name
            import newJobDef
            job = Job.Job()
            job.setJobDef(newJobDef.job)
            logGUID = newJobDef.job.get('logGUID', "")
            if logGUID != "NULL" and logGUID != "":
                job.tarFileGuid = logGUID

            if self.__firstJob:
                job.workdir = self.__jobSite.workdir
                self.__firstJob = False

            self.__firstJobId = job.jobId
            filename = os.path.join(self.__pilotWorkingDir, "Job_%s.json" % job.jobId)
            content = {'workdir': job.workdir, 'data': newJobDef.job, 'experiment': self.__thisExperiment.getExperiment()}
            with open(filename, 'w') as outputFile:
                json.dump(content, outputFile)

            self.__jobStateFile = '%s/jobState-%s.pickle' % (self.__pilotWorkingDir, job.jobId)
        except Exception, e:
            pilotErrorDiag = "Failed to process job info: %s" % str(e)
            tolog("!!WARNING!!3000!! %s" % (pilotErrorDiag))
            self.failOneJob(0, PilotErrors.ERR_UNKNOWN, job, pilotErrorDiag=pilotErrorDiag, final=True, updatePanda=False)
            return -1

        self.setupJob(job, newJobDef.job)

        tolog("Get Event Ranges for job %s" % job.jobId)
        eventRanges = self.getJobEventRanges(job, numRanges=self.__neededEventRanges)
        self.__neededEventRanges = self.__neededEventRanges - len(eventRanges)
        tolog("Get %s Event ranges for job %s" % (len(eventRanges), job.jobId))
        if len(eventRanges) > self.__maxEventsPerJob:
            self.__maxEventsPerJob = len(eventRanges)
        self.__eventRanges[job.jobId] = {}
        self.__jobEventRanges[job.jobId] = eventRanges
        for eventRange in eventRanges:
            self.__eventRanges[job.jobId][eventRange['eventRangeID']] = 'new'

        self.updateJobState(job, 'starting', '', final=False)
        return 0

    def updateJobState(self, job, jobState, hpcState, final=False, updatePanda=True, errorCode=0):
        job.HPCJobId = self.__hpcJobId
        job.setMode(self.__hpcMode)
        job.jobState = jobState
        job.setState([job.jobState, 0, errorCode])
        job.setHpcStatus(hpcState)
        if job.pilotErrorDiag and len(job.pilotErrorDiag.strip()) == 0:
            job.pilotErrorDiag = None
        JR = self.__jobs[job.jobId]['JR']

        pilotErrorDiag = job.pilotErrorDiag

        JR.updateJobStateTest(job, self.__jobSite, self.__node, mode="test")
        rt = RunJobUtilities.updatePilotServer(job, self.getPilotServer(), self.getPilotPort(), final=final)
        if updatePanda:
            JR.updatePandaServer(job, self.__jobSite, self.__node, 25443)
        job.pilotErrorDiag = pilotErrorDiag

    def updateAllJobsState(self, jobState, hpcState, final=False, updatePanda=False):
        for jobId in self.__jobs:
            self.updateJobState(self.__jobs[jobId]['job'], jobState, hpcState, final=final, updatePanda=updatePanda)

    def getHPCEventJobs(self):
        self.getHPCEventJobFromEnv()
        tolog("NJobs: %s" % self.__nJobs)
        failures = 0
        while self.__neededEventRanges > 0 and (len(self.__jobs.keys()) < int(self.__nJobs)):
            tolog("Len(jobs): %s" % len(self.__jobs.keys()))
            tolog("NJobs: %s" % self.__nJobs)
            toGetNJobs = self.__neededEventRanges/self.__maxEventsPerJob
            if toGetNJobs < 1:
                toGetNJobs = 1
            if toGetNJobs > 50:
                toGetNJobs = 50
            tolog("Will try to get NJobs: %s" % toGetNJobs)
            try:
                ret = self.getHPCEventJobFromPanda(nJobs=toGetNJobs)
                if ret != 0:
                    tolog("Failed to get a job from panda.")
                    failures += 1
            except:
                tolog("Failed to get job: %s" % (traceback.format_exc()))
                failures += 1
            if failures > 5:
                break
        self.__hpcStatue = ''
        #self.updateAllJobsState('starting', self.__hpcStatue)

    def stageInOneJob_new(self, job, jobSite, analysisJob, avail_files={}, pfc_name="PoolFileCatalog.xml"):
        """ Perform the stage-in """

        current_dir = self.__pilotWorkingDir
        os.chdir(job.workdir)
        tolog("Start to stage in input files for job %s" % job.jobId)
        tolog("Switch from current dir %s to job %s workdir %s" % (current_dir, job.jobId, job.workdir))

        real_stagein = False
        for lfn in job.inFiles:
            if not (lfn in self.__avail_files):
                real_stagein = True
        if not real_stagein:
            tolog("All files for job %s have copies locally, will try to copy locally" % job.jobId)
            for lfn in job.inFiles:
                try:
                    copy_src = self.__avail_files[lfn]
                    copy_dest = os.path.join(job.workdir, lfn)
                    tolog("Copy %s to %s" % (copy_src, copy_dest))
                    shutil.copyfile(copy_src, copy_dest)
                except:
                    tolog("Failed to copy file: %s" % traceback.format_exc())
                    real_stagein = True
                    break
        if not real_stagein:
            tolog("All files for job %s copied locally" % job.jobId)
            tolog("Switch back from job %s workdir %s to current dir %s" % (job.jobId, job.workdir, current_dir))
            os.chdir(current_dir)
            return job, job.inFiles, None, None


        tolog("Preparing for get command [stageIn_new]")

        infiles = [e.lfn for e in job.inData]

        tolog("Input file(s): (%s in total)" % len(infiles))
        for ind, lfn in enumerate(infiles, 1):
            tolog("%s. %s" % (ind, lfn))

        if not infiles:
            tolog("No input files for this job .. skip stage-in")
            return job, infiles, None, False

        t0 = os.times()

        job.result[2], job.pilotErrorDiag, _dummy, FAX_dictionary = mover.get_data_new(job, jobSite, stageinTries=self.__stageinretry, proxycheck=False, workDir=job.workdir, pfc_name=pfc_name)

        t1 = os.times()

        job.timeStageIn = int(round(t1[4] - t0[4]))

        usedFAXandDirectIO = FAX_dictionary.get('usedFAXandDirectIO', False)

        statusPFCTurl = None

        return job, infiles, statusPFCTurl, usedFAXandDirectIO

    @mover.use_newmover(stageInOneJob_new)
    def stageInOneJob(self, job, jobSite, analysisJob, avail_files={}, pfc_name="PoolFileCatalog.xml"):
        """ Perform the stage-in """

        current_dir = self.__pilotWorkingDir
        os.chdir(job.workdir)
        tolog("Start to stage in input files for job %s" % job.jobId)
        tolog("Switch from current dir %s to job %s workdir %s" % (current_dir, job.jobId, job.workdir))

        real_stagein = False
        for lfn in job.inFiles:
            if not (lfn in self.__avail_files):
                real_stagein = True
        if not real_stagein:
            tolog("All files for job %s have copies locally, will try to copy locally" % job.jobId)
            for lfn in job.inFiles:
                try:
                    copy_src = self.__avail_files[lfn]
                    copy_dest = os.path.join(job.workdir, lfn)
                    tolog("Copy %s to %s" % (copy_src, copy_dest))
                    shutil.copyfile(copy_src, copy_dest)
                except:
                    tolog("Failed to copy file: %s" % traceback.format_exc())
                    real_stagein = True
                    break
        if not real_stagein:
            tolog("All files for job %s copied locally" % job.jobId)
            tolog("Switch back from job %s workdir %s to current dir %s" % (job.jobId, job.workdir, current_dir))
            os.chdir(current_dir)
            return job, job.inFiles, None, None

        ec = 0
        statusPFCTurl = None
        usedFAXandDirectIO = False

        # Prepare the input files (remove non-valid names) if there are any
        ins, job.filesizeIn, job.checksumIn = RunJobUtilities.prepareInFiles(job.inFiles, job.filesizeIn, job.checksumIn)
        if ins:
            tolog("Preparing for get command")

            # Get the file access info (only useCT is needed here)
            useCT, oldPrefix, newPrefix = self.__siteInfo.getFileAccessInfo(job.transferType)

            # Transfer input files
            tin_0 = os.times()
            ec, job.pilotErrorDiag, statusPFCTurl, FAX_dictionary = \
                mover.get_data(job, jobSite, ins, self.__stageinretry, analysisJob=analysisJob, usect=useCT,\
                               pinitdir=self.getPilotInitDir(), proxycheck=False, inputDir='', workDir=job.workdir, pfc_name=pfc_name)
            if ec != 0:
                job.result[2] = ec
            tin_1 = os.times()
            job.timeStageIn = int(round(tin_1[4] - tin_0[4]))

            # Extract any FAX info from the dictionary
            if FAX_dictionary.has_key('N_filesWithoutFAX'):
                job.filesWithoutFAX = FAX_dictionary['N_filesWithoutFAX']
            if FAX_dictionary.has_key('N_filesWithFAX'):
                job.filesWithFAX = FAX_dictionary['N_filesWithFAX']
            if FAX_dictionary.has_key('bytesWithoutFAX'):
                job.bytesWithoutFAX = FAX_dictionary['bytesWithoutFAX']
            if FAX_dictionary.has_key('bytesWithFAX'):
                job.bytesWithFAX = FAX_dictionary['bytesWithFAX']
            if FAX_dictionary.has_key('usedFAXandDirectIO'):
                usedFAXandDirectIO = FAX_dictionary['usedFAXandDirectIO']

        tolog("Switch back from job %s workdir %s to current dir %s" % (job.jobId, job.workdir, current_dir))
        os.chdir(current_dir)

        if ec == 0:
            for inFile in ins:
                self.__avail_files[inFile] = os.path.join(job.workdir, inFile)
        return job, ins, statusPFCTurl, usedFAXandDirectIO

    def failOneJob(self, transExitCode, pilotExitCode, job, ins=None, pilotErrorDiag=None, docleanup=True, final=True, updatePanda=False):
        """ set the fail code and exit """

        current_dir = self.__pilotWorkingDir
        if pilotExitCode and job.attemptNr < 4 and job.eventServiceMerge:
            pilotExitCode = PilotErrors.ERR_ESRECOVERABLE
        job.setState(["failed", transExitCode, pilotExitCode])
        if pilotErrorDiag:
            job.pilotErrorDiag = pilotErrorDiag
        tolog("Job %s failed. Will now update local pilot TCP server" % job.jobId)
        self.updateJobState(job, 'failed', 'failed', final=final, updatePanda=updatePanda)
        if ins:
            ec = pUtil.removeFiles(job.workdir, ins)

        self.cleanup(job)
        sys.stderr.close()
        tolog("Job %s has failed" % job.jobId)
        os.chdir(current_dir)

    def failAllJobs(self, transExitCode, pilotExitCode, jobs, pilotErrorDiag=None, docleanup=True, updatePanda=False):
        firstJob = None
        for jobId in jobs:
            if self.__firstJobId and (jobId == self.__firstJobId):
                firstJob = jobs[jobId]['job']
                continue
            job = jobs[jobId]['job']
            self.failOneJob(transExitCode, pilotExitCode, job, ins=job.inFiles, pilotErrorDiag=pilotErrorDiag, updatePanda=updatePanda)
        if firstJob:
            self.failOneJob(transExitCode, pilotExitCode, firstJob, ins=firstJob.inFiles, pilotErrorDiag=pilotErrorDiag, updatePanda=updatePanda)
        os._exit(pilotExitCode)

    def stageInHPCJobs(self):
        tolog("Setting stage-in state until all input files have been copied")
        jobResult = 0
        pilotErrorDiag = ""
        self.__avail_files = {}
        failedJobIds = []
        for jobId in self.__jobs:
            try:
                job = self.__jobs[jobId]['job']
                # self.updateJobState(job, 'transferring', '')
                self.updateJobState(job, 'starting', '')

                # stage-in all input files (if necessary)
                jobRet, ins, statusPFCTurl, usedFAXandDirectIO = self.stageInOneJob(job, self.__jobSite, self.__jobs[job.jobId]['analysisJob'], self.__avail_files, pfc_name="PFC.xml")
                if jobRet.result[2] != 0:
                    tolog("Failing job with ec: %d" % (jobRet.result[2]))
                    jobResult = jobRet.result[2]
                    pilotErrorDiag = job.pilotErrorDiag
                    failedJobIds.append(jobId)
                    self.failOneJob(0, jobResult, job, ins=job.inFiles, pilotErrorDiag=pilotErrorDiag, updatePanda=True)
                    continue
                    #break
                job.displayJob()
                self.__jobs[job.jobId]['job'] = job
            except:
                tolog("stageInHPCJobsException")
                try:
                    tolog(traceback.format_exc())
                except:
                    tolog("Failed to print traceback")
                job = self.__jobs[jobId]['job']
                jobResult = PilotErrors.ERR_STAGEINFAILED
                pilotErrorDiag = "stageInHPCJobsException"
                failedJobIds.append(jobId)
                self.failOneJob(0, jobResult, job, ins=job.inFiles, pilotErrorDiag=pilotErrorDiag, updatePanda=True)

        for jobId in failedJobIds:
            del self.__jobs[jobId]
        #if jobResult != 0:
        #    self.failAllJobs(0, jobResult, self.__jobs, pilotErrorDiag=pilotErrorDiag)


    def updateEventRange(self, event_range_id, jobid, status='finished', os_bucket_id=-1):
        """ Update an event range on the Event Server """
        message = EventRanges.updateEventRange(event_range_id, [], jobid, status, os_bucket_id)

        return 0, message


    def updateEventRanges(self, event_ranges):
        """ Update an event range on the Event Server """
        return EventRanges.updateEventRanges(event_ranges, url=self.getPanDAServer())

        return status, message

    def getJobEventRanges(self, job, numRanges=2):
        """ Download event ranges from the Event Server """

        tolog("Server: Downloading new event ranges..")

        message = EventRanges.downloadEventRanges(job.jobId, job.jobsetID, job.taskID, numRanges=numRanges, url=self.getPanDAServer())
        try:
            if "Failed" in message or "No more events" in message:
                tolog(message)
                return []
            else:
                return json.loads(message)
        except:
            tolog(traceback.format_exc())
            return []

    def updateHPCEventRanges(self):
        for jobId in self.__eventRanges:
            for eventRangeID in self.__eventRanges[jobId]:
                if self.__eventRanges[jobId][eventRangeID] == 'stagedOut' or self.__eventRanges[jobId][eventRangeID] == 'failed':
                    if self.__eventRanges[jobId][eventRangeID] == 'stagedOut':
                        eventStatus = 'finished'
                    else:
                        eventStatus = 'failed'
                    try:
                        ret, message = self.updateEventRange(eventRangeID, jobId, eventStatus)
                    except Exception, e:
                        tolog("Failed to update event range: %s, %s, exception: %s " % (eventRangeID, eventStatus, str(e)))
                    else:
                        if ret == 0:
                            self.__eventRanges[jobId][eventRangeID] = "Done"
                        else:
                            tolog("Failed to update event range: %s" % eventRangeID)


    def prepareHPCJob(self, job):
        tolog("Prepare for job %s" % job.jobId)
        current_dir = self.__pilotWorkingDir
        os.chdir(job.workdir)
        tolog("Switch from current dir %s to job %s workdir %s" % (current_dir, job.jobId, job.workdir))

        #print self.__runCommandList
        #print self.getParentWorkDir()
        #print self.__job.workdir
        # 1. input files
        inputFiles = []
        inputFilesGlobal = []
        for inputFile in job.inFiles:
            #inputFiles.append(os.path.join(self.__job.workdir, inputFile))
            inputFilesGlobal.append(os.path.join(job.workdir, inputFile))
            inputFiles.append(os.path.join('HPCWORKINGDIR', inputFile))
        inputFileDict = dict(zip(job.inFilesGuids, inputFilesGlobal))
        self.__jobs[job.jobId]['inputFilesGlobal'] = inputFilesGlobal

        tagFiles = {}
        EventFiles = {}
        for guid in inputFileDict:
            if '.TAG.' in inputFileDict[guid]:
                tagFiles[guid] = inputFileDict[guid]
            elif not "DBRelease" in inputFileDict[guid]:
                EventFiles[guid] = {}
                EventFiles[guid]['file'] = inputFileDict[guid]

        # 2. create TAG file
        jobRunCmd = self.__jobs[job.jobId]['runCommandList'][0]
	usingTokenExtractor = 'TokenScatterer' in jobRunCmd or 'UseTokenExtractor=True' in jobRunCmd.replace("  ","").replace(" ","")
        if usingTokenExtractor:
            for guid in EventFiles:
                local_copy = False
                if guid in self.__avail_tag_files:
                    local_copy = True
                    try:
                        tolog("Copy TAG file from %s to %s" % (self.__avail_tag_files[guid]['TAG_path'], job.workdir))
                        shutil.copy(self.__avail_tag_files[guid]['TAG_path'], job.workdir)
                    except:
                        tolog("Failed to copy %s to %s" % (self.__avail_tag_files[guid]['TAG_path'], job.workdir))
                        local_copy = False
                if local_copy:
                    tolog("Tag file for %s already copied locally. Will not create it again" % guid)
                    EventFiles[guid]['TAG'] = self.__avail_tag_files[guid]['TAG']
                    EventFiles[guid]['TAG_guid'] = self.__avail_tag_files[guid]['TAG_guid']
                else:
                    tolog("Tag file for %s does not exist. Will create it." % guid)
                    inFiles = [EventFiles[guid]['file']]
                    input_tag_file, input_tag_file_guid = self.createTAGFile(self.__jobs[job.jobId]['runCommandList'][0], job.trf, inFiles, "MakeRunEventCollection.py")
                    if input_tag_file != "" and input_tag_file_guid != "":
                        tolog("Will run TokenExtractor on file %s" % (input_tag_file))
                        EventFiles[guid]['TAG'] = input_tag_file
                        EventFiles[guid]['TAG_guid'] = input_tag_file_guid
                        self.__avail_tag_files[guid] = {'TAG': input_tag_file, 'TAG_path': os.path.join(job.workdir, input_tag_file), 'TAG_guid': input_tag_file_guid}
                    else:
                        # only for current test
                        if len(tagFiles)>0:
                            EventFiles[guid]['TAG_guid'] = tagFiles.keys()[0]
                            EventFiles[guid]['TAG'] = tagFiles[tagFiles.keys()[0]]
                        else:
                            return -1, "Failed to create the TAG file", None

        # 3. create Pool File Catalog
        inputFileDict = dict(zip(job.inFilesGuids, inputFilesGlobal))
        poolFileCatalog = os.path.join(job.workdir, "PoolFileCatalog_HPC.xml")
        createPoolFileCatalog(inputFileDict, [], pfc_name=poolFileCatalog)
        inputFileDictTemp = dict(zip(job.inFilesGuids, inputFiles))
        poolFileCatalogTemp = os.path.join(job.workdir, "PoolFileCatalog_Temp.xml")
        poolFileCatalogTempName = "HPCWORKINGDIR/PoolFileCatalog_Temp.xml"
        createPoolFileCatalog(inputFileDictTemp, [], pfc_name=poolFileCatalogTemp)
        self.__jobs[job.jobId]['poolFileCatalog'] = poolFileCatalog
        self.__jobs[job.jobId]['poolFileCatalogTemp'] = poolFileCatalogTemp
        self.__jobs[job.jobId]['poolFileCatalogTempName'] = poolFileCatalogTempName

        # 4. getSetupCommand
        setupCommand = self.stripSetupCommand(self.__jobs[job.jobId]['runCommandList'][0], job.trf)
        _cmd = re.search('(source.+\;)', setupCommand)
        source_setup = None
        if _cmd:
            setup = _cmd.group(1)
            source_setup = setup.split(";")[0]
            #setupCommand = setupCommand.replace(source_setup, source_setup + " --cmtextratags=ATLAS,useDBRelease")
            # for test, asetup has a bug
            #new_source_setup = source_setup.split("cmtsite/asetup.sh")[0] + "setup-19.2.0-quick.sh"
            #setupCommand = setupCommand.replace(source_setup, new_source_setup)
        tolog("setup command: " + setupCommand)

        # 5. check if release-compact.tgz exists. If it exists, use it.
        preSetup = None
        postRun = None
        # yoda_setup_command = 'export USING_COMPACT=1; %s' % source_setup

        # 6. AthenaMP command
        runCommandList_0 = self.__jobs[job.jobId]['runCommandList'][0]
        runCommandList_0 = 'export USING_COMPACT=1; %s' % runCommandList_0
        # Tell AthenaMP the name of the yampl channel
        runCommandList_0 = 'export PILOT_EVENTRANGECHANNEL=PILOT_EVENTRANGECHANNEL_CHANGE_ME; %s' % runCommandList_0
        if not "--preExec" in runCommandList_0:
            runCommandList_0 += " --preExec \'from AthenaMP.AthenaMPFlags import jobproperties as jps;jps.AthenaMPFlags.EventRangeChannel=\"PILOT_EVENTRANGECHANNEL_CHANGE_ME\"\' "
        else:
            if "import jobproperties as jps" in runCommandList_0:
                runCommandList_0 = runCommandList_0.replace("import jobproperties as jps;", "import jobproperties as jps;jps.AthenaMPFlags.EventRangeChannel=\"PILOT_EVENTRANGECHANNEL_CHANGE_ME\";")
            else:
                if "--preExec " in runCommandList_0:
                    runCommandList_0 = runCommandList_0.replace("--preExec ", "--preExec \'from AthenaMP.AthenaMPFlags import jobproperties as jps;jps.AthenaMPFlags.EventRangeChannel=\"PILOT_EVENTRANGECHANNEL_CHANGE_ME\"\' ")
                else:
                    tolog("!!WARNING!!43431! --preExec has an unknown format - expected \'--preExec \"\' or \"--preExec \'\", got: %s" % (runCommandList[0]))

        # if yoda_setup_command:
        #     runCommandList_0 = self.__jobs[job.jobId]['runCommandList'][0]
        #     runCommandList_0 = runCommandList_0.replace(source_setup, yoda_setup_command)
        if not self.__copyInputFiles:
            jobInputFileList = None
            # jobInputFileList = inputFilesGlobal[0]
            for inputFile in job.inFiles:
                if not jobInputFileList:
                    jobInputFileList = os.path.join(job.workdir, inputFile)
                else:
                    jobInputFileList += "," + os.path.join(job.workdir, inputFile)
            command_list = runCommandList_0.split(" ")
            command_list_new = []
            for command_part in command_list:
                if command_part.startswith("--input"):
                    command_arg = command_part.split("=")[0]
                    command_part_new = command_arg + "=" + jobInputFileList
                    command_list_new.append(command_part_new)
                else:
                    command_list_new.append(command_part)
            runCommandList_0 = " ".join(command_list_new)


            #runCommandList_0 += " '--postExec' 'svcMgr.PoolSvc.ReadCatalog += [\"xmlcatalog_file:%s\"]'" % (poolFileCatalog)
        else:
            #runCommandList_0 += " '--postExec' 'svcMgr.PoolSvc.ReadCatalog += [\"xmlcatalog_file:%s\"]'" % (poolFileCatalogTempName)
            pass

        # should not have --DBRelease and UserFrontier.py in HPC
        if not os.environ.has_key('Nordugrid_pilot'):
            runCommandList_0 = runCommandList_0.replace("--DBRelease=current", "").replace('--DBRelease="default:current"', '').replace("--DBRelease='default:current'", '')
            if 'RecJobTransforms/UseFrontier.py,' in runCommandList_0:
                runCommandList_0 = runCommandList_0.replace('RecJobTransforms/UseFrontier.py,', '')
            if ',RecJobTransforms/UseFrontier.py' in runCommandList_0:
                runCommandList_0 = runCommandList_0.replace(',RecJobTransforms/UseFrontier.py', '')
            if ' --postInclude=RecJobTransforms/UseFrontier.py ' in runCommandList_0:
                runCommandList_0 = runCommandList_0.replace(' --postInclude=RecJobTransforms/UseFrontier.py ', ' ')
            if '--postInclude "default:RecJobTransforms/UseFrontier.py"' in runCommandList_0:
                runCommandList_0 = runCommandList_0.replace('--postInclude "default:RecJobTransforms/UseFrontier.py"', ' ')
            runCommandList_0 = runCommandList_0.replace('--postInclude "default:PyJobTransforms/UseFrontier.py"', ' ')

        runCommandList_0 += " 1>athenaMP_stdout.txt 2>athenaMP_stderr.txt"
        runCommandList_0 = runCommandList_0.replace(";;", ";")
        #self.__jobs[job.jobId]['runCommandList'][0] = runCommandList_0

        # 7. Token Extractor file list
        # in the token extractor file list, the guid is the Event guid, not the tag guid.
        if usingTokenExtractor:
            tagFile_list = os.path.join(job.workdir, "TokenExtractor_filelist")
            handle = open(tagFile_list, 'w')
            for guid in EventFiles:
                tagFile = EventFiles[guid]['TAG']
                line = guid + ",PFN:" + tagFile + "\n"
                handle.write(line)
            handle.close()
            self.__jobs[job.jobId]['tagFile_list'] = tagFile_list
        else:
            self.__jobs[job.jobId]['tagFile_list'] = None

        # 8. Token Extractor command
        if usingTokenExtractor:
            setup = setupCommand
            tokenExtractorCmd = setup + " TokenExtractor -v  --source " + tagFile_list + " 1>tokenExtract_stdout.txt 2>tokenExtract_stderr.txt"
            tokenExtractorCmd = tokenExtractorCmd.replace(";;", ";").replace("; ;", ";")
            self.__jobs[job.jobId]['tokenExtractorCmd'] = tokenExtractorCmd
        else:
            self.__jobs[job.jobId]['tokenExtractorCmd'] = None

        if self.__yoda_to_zip or self.__es_to_zip:
            self.__jobs[job.jobId]['job'].outputZipName = os.path.join(self.__pilotWorkingDir, "EventService_premerge_%s.tar" % job.jobId)
            self.__jobs[job.jobId]['job'].outputZipEventRangesName = os.path.join(self.__pilotWorkingDir, "EventService_premerge_eventranges_%s.txt" % job.jobId)

        os.chdir(current_dir)
        tolog("Switch back from job %s workdir %s to current dir %s" % (job.jobId, job.workdir, current_dir))


        return 0, None, {"TokenExtractCmd": self.__jobs[job.jobId]['tokenExtractorCmd'], "AthenaMPCmd": runCommandList_0, "PreSetup": preSetup, "PostRun": postRun, 'PoolFileCatalog': poolFileCatalog, 'InputFiles': inputFilesGlobal, 'GlobalWorkingDir': job.workdir, 'zipFileName': self.__jobs[job.jobId]['job'].outputZipName, 'zipEventRangesName': self.__jobs[job.jobId]['job'].outputZipEventRangesName, 'stageout_threads': self.__stageout_threads}

    def prepareHPCJobs(self):
        for jobId in self.__jobs:
            try:
                status, output, hpcJob = self.prepareHPCJob(self.__jobs[jobId]['job'])
            except:
                tolog("Failed to prepare HPC Job: %s" % jobId)
                self.__jobs[jobId]['hpcJob'] = None
                continue
            tolog("HPC Job %s: %s " % (jobId, hpcJob))
            if status == 0:
                self.__jobs[jobId]['hpcJob'] = hpcJob
            else:
                return status, output
        return 0, None

    def getJobDatasets(self, job):
        """ Get the datasets for the output files """

        # Get the default dataset
        if job.destinationDblock and job.destinationDblock[0] != 'NULL' and job.destinationDblock[0] != ' ':
            dsname = job.destinationDblock[0]
        else:
            dsname = "%s-%s-%s" % (time.localtime()[0:3]) # pass it a random name

        # Create the dataset dictionary
        # (if None, the dsname above will be used for all output files)
        datasetDict = getDatasetDict(job.outFiles, job.destinationDblock, job.logFile, job.logDblock)
        if datasetDict:
            tolog("Dataset dictionary has been verified: %s" % str(datasetDict))
        else:
            tolog("Dataset dictionary could not be verified, output files will go to: %s" % (dsname))

        return dsname, datasetDict

    def setupJobStageOutHPCEvent(self, job):
        if job.prodDBlockTokenForOutput is not None and len(job.prodDBlockTokenForOutput) > 0 and job.prodDBlockTokenForOutput[0] != 'NULL':
            siteInfo = getSiteInformation(self.getExperiment())
            objectstore_orig = siteInfo.readpar("objectstore")
            #siteInfo.replaceQueuedataField("objectstore", self.__job.prodDBlockTokenForOutput[0])
        else:
            #siteInfo = getSiteInformation(self.getExperiment())
            #objectstore = siteInfo.readpar("objectstore")
            pass
        # espath = getFilePathForObjectStore(filetype="eventservice")
        siteInfo = getSiteInformation(self.getExperiment())
        ddmendpoint = siteInfo.getObjectstoreDDMEndpoint(os_bucket_name='eventservice')
        os_bucket_id = siteInfo.getObjectstoreBucketID(ddmendpoint)
        tolog("Will use the default bucket ID: %s" % (os_bucket_id))
        espath = siteInfo.getObjectstorePath(os_bucket_id=os_bucket_id, label='w')
        tolog("EventServer objectstore path: " + espath)

        siteInfo = getSiteInformation(self.getExperiment())
        # get the copy tool
        setup = siteInfo.getCopySetup(stageIn=False)
        tolog("Copy Setup: %s" % (setup))

        dsname, datasetDict = self.getJobDatasets(job)
        report = getInitialTracingReport(userid=job.prodUserID, sitename=self.__jobSite.sitename, dsname=dsname, eventType="objectstore", analysisJob=self.__analysisJob, jobId=self.__job.jobId, jobDefId=self.__job.jobDefinitionID, dn=self.__job.prodUserID)
        self.__siteMover = objectstoreSiteMover(setup)


    def stageOutHPCEvent(self, output_info):
        eventRangeID, status, output = output_info
        self.__output_es_files.append(output)

        if status == 'failed':
            try:
                self.__eventRanges[eventRangeID] = 'failed'
            except Exception, e:
                tolog("!!WARNING!!2233!! update %s:%s threw an exception: %s" % (eventRangeID, 'failed', e))
        if status == 'finished':
            status, pilotErrorDiag, surl, size, checksum, self.arch_type = self.__siteMover.put_data(output, self.__espath, lfn=os.path.basename(output), report=self.__report, token=self.__job.destinationDBlockToken, experiment=self.__job.experiment)
            if status == 0:
                try:
                    #self.updateEventRange(eventRangeID)
                    self.__eventRanges[eventRangeID] = 'stagedOut'
                    tolog("Remove staged out output file: %s" % output)
                    os.remove(output)
                except Exception, e:
                    tolog("!!WARNING!!2233!! remove ouput file threw an exception: %s" % (e))
                    #self.__failedStageOuts.append(output_info)
                else:
                    tolog("remove output file has returned")
            else:
                tolog("!!WARNING!!1164!! Failed to upload file to objectstore: %d, %s" % (status, pilotErrorDiag))
                self.__failedStageOuts.append(output_info)


    def startHPCJobs(self):
        tolog("startHPCJobs")
        self.__hpcStatue = 'starting'
        self.updateAllJobsState('starting', self.__hpcStatue)

        status, output = self.prepareHPCJobs()
        if status != 0:
            tolog("Failed to prepare HPC jobs: status %s, output %s" % (status, output))
            self.failAllJobs(0, PilotErrors.ERR_UNKNOWN, self.__jobs, pilotErrorDiag=output)
            return 

        # setup stage out
        #self.setupStageOutHPCEvent()

        self.__hpcStatus = None
        self.__hpcLog = None

        hpcManager = self.__hpcManager
        totalCores = hpcManager.getCoreCount()
        totalJobs = len(self.__jobs.keys())
        if totalJobs < 1:
            totalJobs = 1
        avgCores = totalCores / totalJobs
        hpcJobs = {}
        for jobId in self.__jobs:
            self.__jobs[jobId]['job'].coreCount = avgCores
            if len(self.__eventRanges[jobId]) > 0 and 'hpcJob' in self.__jobs[jobId] and self.__jobs[jobId]['hpcJob']:
                hpcJobs[jobId] = self.__jobs[jobId]['hpcJob']
        hpcManager.initJobs(hpcJobs, self.__jobEventRanges)

        totalCores = hpcManager.getCoreCount()
        avgCores = totalCores / totalJobs
        for jobId in self.__jobs:
            self.__jobs[jobId]['job'].coreCount = avgCores

        if hpcManager.isLocalProcess():
            self.__hpcStatue = 'running'
            self.updateAllJobsState('running', self.__hpcStatue)

        tolog("Submit HPC job")
        hpcManager.submit()
        tolog("Submitted HPC job")
        # create file with batchid in name for reference
        with open(self.getPilotInitDir() + '/batchid.' + str(hpcManager.getHPCJobId()) + '.txt','w') as file:
            file.write(str(hpcManager.getHPCJobId()))
            file.close()
        if hpcManager.isLocalProcess():
            self.__hpcStatue = 'closed'
            self.updateAllJobsState('transferring', self.__hpcStatue)

        hpcManager.setPandaJobStateFile(self.__jobStateFile)
        #self.__stageout_threads = defRes['stageout_threads']
        hpcManager.setStageoutThreads(self.__stageout_threads)
        hpcManager.saveState()
        self.__hpcManager = hpcManager

    def startHPCSlaveJobs(self):
        tolog("Setup HPC Manager")
        hpcManager = HPCManager(globalWorkingDir=self.__pilotWorkingDir)
        tolog("Submit HPC job")
        hpcManager.submit()
        tolog("Submitted HPC job")

    def runHPCEvent(self):
        tolog("runHPCEvent")
        threadpool = ThreadPool(self.__stageout_threads)
        hpcManager = self.__hpcManager

        try:
            old_state = None
            time_start = time.time()
            while not hpcManager.isFinished():
                state = hpcManager.poll()
                self.__job.setHpcStatus(state)
                if old_state is None or old_state != state or time.time() > (time_start + 60*10):
                    old_state = state
                    time_start = time.time()
                    tolog("HPCManager Job stat: %s" % state)
                    self.__JR.updateJobStateTest(self.__job, self.__jobSite, self.__node, mode="test")
                    rt = RunJobUtilities.updatePilotServer(self.__job, self.getPilotServer(), self.getPilotPort())
                    self.__JR.updatePandaServer(self.__job, self.__jobSite, self.__node, 25443)

                if state and state == 'Running':
                    self.__job.jobState = "running"
                    self.__job.setState([self.__job.jobState, 0, 0])
                if state and state == 'Complete':
                    break
                outputs = hpcManager.getOutputs()
                for output in outputs:
                    #self.stageOutHPCEvent(output)
                    threadpool.add_task(self.stageOutHPCEvent, output)

                time.sleep(30)
                self.updateHPCEventRanges()

            tolog("HPCManager Job Finished")
            self.__job.setHpcStatus('stagingOut')
            rt = RunJobUtilities.updatePilotServer(self.__job, self.getPilotServer(), self.getPilotPort())
            self.__JR.updatePandaServer(self.__job, self.__jobSite, self.__node, 25443)
        except:
            tolog("RunHPCEvent failed: %s" % traceback.format_exc())

        for i in range(3):
            try:
                tolog("HPC Stage out outputs retry %s" % i)
                hpcManager.flushOutputs()
                outputs = hpcManager.getOutputs()
                for output in outputs:
                    #self.stageOutHPCEvent(output)
                    threadpool.add_task(self.stageOutHPCEvent, output)

                self.updateHPCEventRanges()
                threadpool.wait_completion()
                self.updateHPCEventRanges()
            except:
                tolog("RunHPCEvent stageout outputs retry %s failed: %s" % (i, traceback.format_exc()))

        for i in range(3):
            try:
                tolog("HPC Stage out failed outputs retry %s" % i)
                failedStageOuts = self.__failedStageOuts
                self.__failedStageOuts = []
                for failedStageOut in failedStageOuts:
                    threadpool.add_task(self.stageOutHPCEvent, failedStageOut)
                threadpool.wait_completion()
                self.updateHPCEventRanges()
            except:
                tolog("RunHPCEvent stageout failed outputs retry %s failed: %s" % (i, traceback.format_exc()))

        self.__job.setHpcStatus('finished')
        self.__JR.updatePandaServer(self.__job, self.__jobSite, self.__node, 25443)
        self.__hpcStatus, self.__hpcLog = hpcManager.checkHPCJobLog()
        tolog("HPC job log status: %s, job log error: %s" % (self.__hpcStatus, self.__hpcLog))
        
    def getJobMetrics(self):
        try:
            jobMetrics = None
            jobMetricsFileName = "jobMetrics-yoda.json"
            jobMetricsFile = os.path.join(self.__pilotWorkingDir, jobMetricsFileName)
            if not os.path.exists(jobMetricsFile):
                tolog("Yoda job metrics file %s doesn't exist" % jobMetricsFile)
            else:
                file = open(jobMetricsFile)
                try:
                    jobMetrics = json.load(file)
                except:
                    tolog("Failed to load job Metrics: %s" % traceback.format_exc())
                    tolog("Check backup jobmetrics file")
                    file.close()
                    if os.path.exists(jobMetricsFile + ".backup"):
                        file = open(jobMetricsFile + ".backup")
                        jobMetrics = json.load(file)
                        file.close()
                return jobMetrics
        except:
            tolog("Failed to load job Metrics: %s" % traceback.format_exc())
        return None

    def getJobsTimestamp(self):
        try:
            jobMetricsFileName = "jobsTimestamp-yoda.json" 
            jobMetricsFile = os.path.join(self.__pilotWorkingDir, jobMetricsFileName)
            if not os.path.exists(jobMetricsFile):
                tolog("Yoda jobs' timestamp file %s doesn't exist" % jobMetricsFile)
            else:
                file = open(jobMetricsFile)
                jobMetrics = json.load(file)
                return jobMetrics
        except:
            tolog("Failed to load jobs' timestamp: %s" % traceback.format_exc())
        return None

    def checkJobMetrics(self):
        try:
            jobMetrics = self.getJobMetrics()
            jobsTimestamp = self.getJobsTimestamp()

            for jobId in self.__jobs:
                if jobMetrics and jobId in jobMetrics:
                    self.__jobs[jobId]['job'].cpuConsumptionUnit = 's'
                    coreCount = self.__jobs[jobId]['job'].coreCount
                    if coreCount < 1:
                        coreCount = 0
                    self.__jobs[jobId]['job'].coreCount = jobMetrics[jobId]['collect']['cores']
                    self.__jobs[jobId]['job'].cpuConsumptionTime = jobMetrics[jobId]['collect']['cpuConsumptionTime']
                    self.__jobs[jobId]['job'].timeExe = jobMetrics[jobId]['collect']['avgYodaRunningTime']
                    self.__jobs[jobId]['job'].timeSetup = jobMetrics[jobId]['collect']['avgYodaSetupTime']
                    self.__jobs[jobId]['job'].timeStageOut = jobMetrics[jobId]['collect']['avgYodaStageoutTime']
                    self.__jobs[jobId]['job'].nEvents = jobMetrics[jobId]['collect']['totalQueuedEvents']
                    self.__jobs[jobId]['job'].nEventsW = jobMetrics[jobId]['collect']['totalProcessedEvents']
                    self.__jobs[jobId]['job'].yodaJobMetrics = jobMetrics[jobId]['collect']
                    job = self.__jobs[jobId]['job']
                    job.cpuConversionFactor = 1
                    tolog("Job CPU usage: %s %s" % (job.cpuConsumptionTime, job.cpuConsumptionUnit))
                    tolog("Job CPU conversion factor: %1.10f" % (job.cpuConversionFactor))
                else:
                    self.__jobs[jobId]['job'].cpuConsumptionUnit = 's'
                    self.__jobs[jobId]['job'].coreCount = 0
                    self.__jobs[jobId]['job'].cpuConsumptionTime = 0
                    self.__jobs[jobId]['job'].timeExe = 0
                    self.__jobs[jobId]['job'].timeSetup = 0
                    self.__jobs[jobId]['job'].timeStageOut = 0
                    self.__jobs[jobId]['job'].nEvents = 0
                    self.__jobs[jobId]['job'].nEventsW = 0
                    self.__jobs[jobId]['job'].yodaJobMetrics = {'startTime': time.time(), 'endTime': time.time()}
                    job = self.__jobs[jobId]['job']
                    job.cpuConversionFactor = 1
                    tolog("Job CPU usage: %s %s" % (job.cpuConsumptionTime, job.cpuConsumptionUnit))
                    tolog("Job CPU conversion factor: %1.10f" % (job.cpuConversionFactor))

                if jobsTimestamp and jobId in jobsTimestamp:
                    if not self.__jobs[jobId]['job'].yodaJobMetrics:
                        self.__jobs[jobId]['job'].yodaJobMetrics = {}
                    self.__jobs[jobId]['job'].yodaJobMetrics['startTime'] = jobsTimestamp[jobId]['startTime']
                    self.__jobs[jobId]['job'].yodaJobMetrics['endTime'] = jobsTimestamp[jobId]['endTime']
                    if jobsTimestamp[jobId]['startTime']:
                        if not jobsTimestamp[jobId]['endTime']:
                            if not self.__jobs[jobId]['job'].jobState == 'running':
                                self.updateJobState(self.__jobs[jobId]['job'], 'running', 'running', final=False, updatePanda=True)
                        else:
                            if self.__jobs[jobId]['job'].jobState == 'running':
                                self.updateJobState(self.__jobs[jobId]['job'], 'transferring', 'finished', final=False, updatePanda=True)
        except:
            tolog("Failed in check job metrics: %s" % traceback.format_exc())

    def zipOutputs(self, job, zipEventRangeName, zipFileName):
        eventstatus = str(job.jobId) + "_event_status.dump"
        if os.path.exists(eventstatus + ".zipped"):
            tolog("Event status dump file %s exist. It's already zipped." % eventstatus + ".zipped")
            return
        if not os.path.exists(eventstatus):
            tolog("Event status dump file %s doesn't exist. checking backup file" % eventstatus)
            eventstatus = eventstatus + ".backup"
            if not os.path.exists(eventstatus):
                tolog("Event status backup dump file %s doesn't exist." % eventstatus)
                return

        file = open(eventstatus)
        import tarfile
        tar = tarfile.open(zipFileName, 'w')
        zipEventRange = open(zipEventRangeName, 'w')

        tolog("Creating zip/tar file: %s" % zipFileName)
        for line in file:
            #tolog("line: %s" % line)
            try:
                # jobId, eventRangeID,status,output = line.split(" ")
                jobId = line.split(" ")[0]
                eventRangeID = line.split(" ")[1]
                status = line.split(" ")[2]
                output = line.split(" ")[3]
                if status.startswith("ERR"):
                    status = 'failed'
            except:
                tolog("Failed to parse %s at line: %s: %s" % (eventstatus, line, traceback.format_exc()))
            if status == 'failed':
                zipEventRange.write("%s %s %s\n" % (eventRangeID, status, output))
            if not status == 'finished':
                continue
            outputs = output.split(",")[:-3]
            for out in outputs:
                #tolog("Adding file: %s" % out)
                if not os.path.exists(out):
                    tolog("File %s doesn't exist" % out)
                    continue
                tar.add(out, arcname=os.path.basename(out))
                os.remove(out)
            zipEventRange.write("%s %s %s\n" % (eventRangeID, status, output))
        tar.close()
        zipEventRange.close()
        tolog("Zip finished, Rename %s to %s" % (eventstatus, eventstatus + ".zipped"))
        os.rename(eventstatus, eventstatus + ".zipped")

    def stageOutZipFile(self, job, espath, os_bucket_id):
        try:
            dsname, datasetDict = self.getJobDatasets(job)

            tolog("Checking zip status of job %s" % job.jobId)
            zipFileName = job.outputZipName
            zipEventRangeName = job.outputZipEventRangesName
            tolog("Checking zip file: %s" % zipFileName)
            if self.__es_to_zip:
                self.zipOutputs(job, zipEventRangeName, zipFileName)

            if zipFileName is None or (not os.path.exists(zipFileName)):
                tolog("Zip file %s doesn't exits, will not stage out." % (zipFileName))
                return
            if  zipEventRangeName is None or (not os.path.exists(zipEventRangeName)):
                tolog("Zip event ranges file %s doesn't exits, will not stage out." % (zipEventRangeName))
                return

            if self.__copyOutputToGlobal:
                outputDir = os.path.dirname(os.path.dirname(zipFileName))
                tolog("Moving tar/zip file %s to %s" % (zipFileName, os.path.join(outputDir, os.path.basename(zipFileName))))
                os.rename(zipFileName, os.path.join(outputDir, os.path.basename(zipFileName)))
                tolog("Copying tar/zip file %s to %s" % (zipEventRangeName, os.path.join(outputDir, os.path.basename(zipEventRangeName))))
                shutil.copyfile(zipEventRangeName, os.path.join(outputDir, os.path.basename(zipEventRangeName)))
                eventstatusFile = str(job.jobId) + "_event_status.dump.zipped"
                tolog("Copying dump file %s to %s" % (eventstatusFile, os.path.join(outputDir, os.path.basename(eventstatusFile))))
                shutil.copyfile(eventstatusFile, os.path.join(outputDir, os.path.basename(eventstatusFile)))

                jobMetricsFileName = "jobMetrics-yoda.json"
                jobMetricsFile = os.path.join(self.__pilotWorkingDir, jobMetricsFileName)
                if os.path.exists(jobMetricsFile):
                    tolog("Copying job metrics file %s to %s" % (jobMetricsFile, os.path.join(outputDir, os.path.basename(jobMetricsFile))))
                    shutil.copyfile(jobMetricsFile, os.path.join(outputDir, os.path.basename(jobMetricsFile)))

                return

            report = getInitialTracingReport(userid=job.prodUserID, sitename=self.__jobSite.sitename, dsname=dsname, eventType="objectstore", analysisJob=False, jobId=job.jobId, jobDefId=job.jobDefinitionID, dn=job.prodUserID)
            ret_status, pilotErrorDiag, surl, size, checksum, arch_type = self.__siteMover.put_data(zipFileName, espath, lfn=os.path.basename(zipFileName), report=report, token=None, experiment='ATLAS')
            if ret_status == 0:
                eventRanges = []
                self.__jobs[job.jobId]['job'].outputZipBucketID = os_bucket_id
                self.updateJobState(job, 'transferring', 'stagingOut', final=False, updatePanda=True)
                dumpFile = job.outputZipEventRangesName
                file = open(dumpFile)
                for line in file:
                    line = line.strip()
                    if len(line):
                        eventRangeID = line.split(" ")[0]
                        eventStatus = line.split(" ")[1]
                        eventRanges.append({'eventRangeID': eventRangeID, 'eventStatus': eventStatus, 'objstoreID': os_bucket_id})
                if job.yodaJobMetrics and 'totalProcessedEvents' in job.yodaJobMetrics:
                    job.yodaJobMetrics['totalProcessedEvents'] = len(eventRanges)
                    job.nEventsW = job.yodaJobMetrics['totalProcessedEvents']
                for chunkEventRanges in pUtil.chunks(eventRanges, 100):
                    tolog("Update event ranges: %s" % chunkEventRanges)
                    try:
                        status, output = self.updateEventRanges(chunkEventRanges)
                        tolog("Update Event ranges status: %s, output: %s" % (status, output))
                    except:
                        tolog("Failed to update EventRanges: %s" % traceback.format_exc())
                        try:
                            status, output = self.updateEventRanges(chunkEventRanges)
                            tolog("Update Event ranges status: %s, output: %s" % (status, output))
                        except:
                            tolog("Failed to update EventRanges: %s" % traceback.format_exc())
                command = "rm -f %s" % zipFileName
                tolog("delete zip file: %s" % command)
                status, output = commands.getstatusoutput(command)
                tolog("status: %s, output: %s" % (status, output))
            else:
                tolog("Failed to stageout %s: %s" % (zipFileName, pilotErrorDiag))
        except:
            tolog("Failed to stageout zip file for job %s: %s" % (job.jobId, traceback.format_exc()))

    def stageOutZipFiles(self):
        try:
            siteInfo = getSiteInformation(self.getExperiment())
            # get the copy tool
            setup = siteInfo.getCopySetup(stageIn=False)
            tolog("Copy Setup: %s" % (setup))
            #espath = getFilePathForObjectStore(filetype="eventservice")
            ddmendpoint = siteInfo.getObjectstoreDDMEndpoint(os_bucket_name='eventservice')
            os_bucket_id = siteInfo.getObjectstoreBucketID(ddmendpoint)
            tolog("Will use the default bucket ID: %s" % (os_bucket_id))
            espath = siteInfo.getObjectstorePath(os_bucket_id=os_bucket_id, label='w')
            tolog("ES path: %s" % (espath))
            os_bucket_id = siteInfo.getObjectstoresField('os_bucket_id', 'eventservice')
            tolog("Will create a list using the default bucket ID: %s for queue %s" % (os_bucket_id, self.__jobSite.computingElement))

            self.__siteMover = objectstoreSiteMover(setup)
            threadpool = ThreadPool(self.__stageout_threads)

            for jobId in self.__jobs:
                try:
                    """
                    tolog("Checking zip status of job %s" % jobId)
                    zipFileName = self.__jobs[jobId]['job'].outputZipName
                    #zipFile = os.path.join(self.__jobs[jobId]['job'].workdir, zipFileName)
                    zipEventRangeName = self.__jobs[jobId]['job'].outputZipEventRangesName
                    tolog("Checking zip file: %s" % zipFileName)
                    if zipFileName is None or (not os.path.exists(zipFileName)):
                        tolog("Zip file %s doesn't exits, will not stage out." % (zipFileName))
                        continue
                    if  zipEventRangeName is None or (not os.path.exists(zipEventRangeName)):
                        tolog("Zip event ranges file %s doesn't exits, will not stage out." % (zipEventRangeName))
                        continue
                    """
                    threadpool.add_task(self.stageOutZipFile, self.__jobs[jobId]['job'], espath, os_bucket_id)
                    
                except:
                    tolog("Failed to stageout zip files: %s" % (traceback.format_exc()))
            threadpool.wait_completion()
        except:
            tolog("Failed to stageout zip files: %s" % traceback.format_exc())

    def runHPCEventJobsWithEventStager(self, useEventStager=False):
        tolog("runHPCEventWithEventStager")
        hpcManager = self.__hpcManager
        if useEventStager:
            tolog("EventStager is deprecated. Doesn't support eventstager anymore.")

        try:
            old_state = None
            time_start = time.time()
            state = hpcManager.poll()
            self.__hpcStatue = state
            self.__hpcJobId = hpcManager.getHPCJobId()
            #if state and state == 'Running':
            #    self.updateAllJobsState('running', self.__hpcStatue, updatePanda=True)
            #else:
            #    self.updateAllJobsState('starting', self.__hpcStatue, updatePanda=True)

            while not hpcManager.isFinished():
                state = hpcManager.poll()
                self.__hpcStatue = state
                if old_state is None or old_state != state or time.time() > (time_start + 60*10):
                    time_start = time.time()
                    tolog("HPCManager Job stat: %s" % state)
                    self.checkJobMetrics()
                #if state and state == 'Running' and state != old_state:
                #    self.updateAllJobsState('running', self.__hpcStatue, updatePanda=True)

                old_state = state

                if state and state == 'Complete':
                    break

                time.sleep(30)

            tolog("HPCManager Job Finished")
            self.__hpcStatue = 'stagingOut'
            self.checkJobMetrics()
            self.updateAllJobsState('transferring', self.__hpcStatue)
        except:
            tolog("RunHPCEvent failed: %s" % traceback.format_exc())

        self.stageOutZipFiles()
        self.__stageout_status = True

        try:
            hpcManager.postRun()
        except:
            tolog("HPCManager postRun: %s" % traceback.format_exc())

        self.__hpcStatue = 'finished'
        self.updateAllJobsState('transferring', self.__hpcStatue)
        self.__hpcStatus, self.__hpcLog = hpcManager.checkHPCJobLog()
        tolog("HPC job log status: %s, job log error: %s" % (self.__hpcStatus, self.__hpcLog))


    def check_unmonitored_jobs(self):
        all_jobs = {}
        all_files = os.listdir(self.__pilotWorkingDir)
        for file in all_files:
            if re.search('Job_[0-9]+.json', file):
                filename = os.path.join(self.__pilotWorkingDir, file)
                jobId = file.replace("Job_", "").replace(".json", "")
                all_jobs[jobId] = filename
        pUtil.tolog("Found jobs: %s" % all_jobs)
        for jobId in all_jobs:
            try:
                with open(all_jobs[jobId]) as inputFile:
                    content = json.load(inputFile)
                job = Job.Job()
                job.setJobDef(content['data'])
                job.workdir = content['workdir']
                job.experiment = content['experiment']
                runCommandList = content.get('runCommandList', [])
                logGUID = content['data'].get('logGUID', "")
                if logGUID != "NULL" and logGUID != "":
                    job.tarFileGuid = logGUID
                if job.prodUserID:
                    self.__userid = job.prodUserID
                job.outFiles = []

                if (not job.workdir) or (not os.path.exists(job.workdir)):
                    pUtil.tolog("Job %s work dir %s doesn't exit, will not add it to monitor" % (job.jobId, job.workdir))
                    continue
                if jobId not in self.__jobs:
                    self.__jobs[jobId] = {}
                self.__jobs[jobId]['job'] = job
                self.__jobs[job.jobId]['JR'] = JobRecovery(pshttpurl='https://pandaserver.cern.ch', pilot_initdir=job.workdir)
                self.__jobs[job.jobId]['runCommandList'] = runCommandList
                self.__eventRanges[job.jobId] = {}
            except:
                pUtil.tolog("Failed to load unmonitored job %s: %s" % (jobId, traceback.format_exc()))

    def recoveryJobs(self):
        tolog("Start to recovery job.")
        job_state_file = self.getJobStateFile()
        JS = JobState()
        JS.get(job_state_file)
        _job, _site, _node, _recoveryAttempt = JS.decode()
        #self.__job = _job
        self.__jobs[_job.jobId] = {'job': _job}
        self.__jobSite = _site

        # set node info
        self.__node = Node.Node()
        self.__node.setNodeName(os.uname()[1])
        self.__node.collectWNInfo(self.__jobSite.workdir)

        tolog("The job state is %s" % _job.jobState)
        if _job.jobState in ['starting', 'transfering']:
            tolog("The job hasn't started to run")
            # return False

        os.chdir(self.__jobSite.workdir)
        self.__jobs[_job.jobId]['JR'] = JobRecovery(pshttpurl='https://pandaserver.cern.ch', pilot_initdir=_job.workdir)
        self.__pilotWorkingDir = os.path.dirname(_job.workdir)
        self.__siteInfo = getSiteInformation(self.getExperiment())
        self.__userid = "HPCEventRecovery"

        self.check_unmonitored_jobs()

        return True

    def recoveryHPCManager(self):
        logFileName = None
        tolog("Recover Lost HPC Event job")
        tolog("runJobHPCEvent.getPilotLogFilename=%s"% self.getPilotLogFilename())
        if self.getPilotLogFilename() != "":
            logFileName = self.getPilotLogFilename()
        hpcManager = HPCManager(globalWorkingDir=self.__pilotWorkingDir, logFileName=logFileName)
        hpcManager.recoveryState()
        self.__hpcManager = hpcManager
        self.__stageout_threads = hpcManager.getStageoutThreads()

    def finishOneJob(self, job):
        tolog("Finishing job %s" % job.jobId)
        current_dir = self.__pilotWorkingDir
        os.chdir(job.workdir)
        tolog("Switch from current dir %s to job %s workdir %s" % (current_dir, job.jobId, job.workdir))

        pilotErrorDiag = ""
        if job.inFiles:
            ec = pUtil.removeFiles(job.workdir, job.inFiles)
        if job.outputZipName:
            ec = pUtil.removeFiles(job.workdir, [job.outputZipName])
        #if self.__output_es_files:
        #    ec = pUtil.removeFiles("/", self.__output_es_files)


        errorCode = PilotErrors.ERR_UNKNOWN
        if job.attemptNr < 10:
            errorCode = PilotErrors.ERR_ESRECOVERABLE

        if (not job.jobId in self.__eventRanges) or len(self.__eventRanges[job.jobId]) == 0:
            tolog("Cannot get event ranges")
            pilotErrorDiag = "Cannot get event ranges"
            # self.failOneJob(0, errorCode, job, pilotErrorDiag="Cannot get event ranges", final=True, updatePanda=False)
            # return -1
        else:
            if job.jobId in self.__eventRanges:
                eventRanges = self.__eventRanges[job.jobId]
                # check whether all event ranges are handled
                tolog("Total event ranges: %s" % job.nEvents)
                # not_handled_events = eventRanges.values().count('new')
                # tolog("Not handled events: %s" % not_handled_events)
                # done_events = eventRanges.values().count('Done')
                tolog("Finished events: %s" % job.nEventsW)
                #stagedOut_events = eventRanges.values().count('stagedOut')
                #tolog("stagedOut but not updated to panda server events: %s" % stagedOut_events)
                #if done_events + stagedOut_events:
                #    errorCode = PilotErrors.ERR_ESRECOVERABLE
                if job.nEvents - job.nEventsW:
                    tolog("Not all event ranges are handled. failed job")
                    # self.failOneJob(0, errorCode, job, pilotErrorDiag="Not All events are handled(total:%s, left:%s)" % (len(eventRanges), not_handled_events + stagedOut_events), final=True, updatePanda=False)
                    # return -1
                    pilotErrorDiag="Not All events are handled(total:%s, left:%s)" % (job.nEvents, job.nEventsW)

                # Panda only record nEvents
                job.nEvents = job.nEventsW

        dsname, datasetDict = self.getJobDatasets(job)
        tolog("dsname = %s" % (dsname))
        tolog("datasetDict = %s" % (datasetDict))

        # Create the output file dictionary needed for generating the metadata
        ec, pilotErrorDiag, outs, outsDict = RunJobUtilities.prepareOutFiles(job.outFiles, job.logFile, job.workdir, fullpath=True)
        if ec:
            # missing output file (only error code from prepareOutFiles)
            # self.failOneJob(job.result[1], ec, job, pilotErrorDiag=pilotErrorDiag)
            errorCode = ec
            pilotErrorDiag += pilotErrorDiag
        tolog("outsDict: %s" % str(outsDict))

        # Create metadata for all successfully staged-out output files (include the log file as well, even if it has not been created yet)
        ec, job, outputFileInfo = self.createFileMetadata([], job, outsDict, dsname, datasetDict, self.__jobSite.sitename)
        if ec:
            # self.failOneJob(0, ec, job, pilotErrorDiag=job.pilotErrorDiag)
            errorCode = ec
            pilotErrorDiag += job.pilotErrorDiag
            self.failOneJob(0, ec, job, pilotErrorDiag=pilotErrorDiag, final=True, updatePanda=False)
            return -1

        # Rename the metadata produced by the payload
        # if not pUtil.isBuildJob(outs):
        self.moveTrfMetadata(job.workdir, job.jobId)

        # Check the job report for any exit code that should replace the res_tuple[0]
        # res0, exitAcronym, exitMsg = self.getTrfExitInfo(0, job.workdir)
        # res = (res0, exitMsg, exitMsg)

        # Payload error handling
        # ed = ErrorDiagnosis()
        # job = ed.interpretPayload(job, res, False, 0, self.__jobs[job.jobId]['runCommandList'], self.getFailureCode())
        # if job.result[1] != 0 or job.result[2] != 0:
        #     self.failOneJob(job.result[1], job.result[2], job, pilotErrorDiag=job.pilotErrorDiag, final=True, updatePanda=False)
        #     return -1

        if job.nEvents == 0:
            job.pilotErrorDiag = "Over subscribed events"
            self.updateJobState(job, "failed", "finished", final=False, errorCode=PilotErrors.ERR_OVERSUBSCRIBEDEVENTS)
        else:
            self.updateJobState(job, "finished", "finished", final=False)

        tolog("Panda Job %s Done" % job.jobId)
        #self.sysExit(self.__job)
        self.cleanup(job)
        tolog("Switch back from job %s workdir %s to current dir %s" % (job.jobId, job.workdir, current_dir))
        os.chdir(current_dir)
        tolog("Finished job %s" % job.jobId)

    def ignore_files(self, dir, files):
        result = []
        for f in files:
            if f.startswith("sqlite"):
                result.append(f)
        tolog("Ignore files: %s" % result)
        return result

    def copyLogFilesToJob(self):
        found_dirs = {}
        found_files = {}
        all_files = os.listdir(self.__pilotWorkingDir)
        for file in all_files:
            if "job.log.tgz." in file or "LOCKFILE" in file or "tarball_PandaJob" in file:
                continue
            path = os.path.join(self.__pilotWorkingDir, file)
            if os.path.isdir(path):
                if file not in ['HPC', 'lib', 'radical', 'saga'] and not file.startswith("PandaJob_"):
                    if file == 'rank_0' or not file.startswith('ranksd_'):
                        found_dirs[file] = path
                        tolog("Found log dir %s" % path)
            else:
                if not (file.endswith(".py") or file.endswith(".pyc")):
                    found_files[file] = path
                    tolog("Found log file %s" % path)

        for jobId in self.__jobs:
            job = self.__jobs[jobId]['job']
            tolog("Copy log files to job %s work dir %s" % (jobId, job.workdir))
            for file in found_dirs:
                path = found_dirs[file]
                dest_dir = os.path.join(job.workdir, file)
                try:
                    if file == 'rank_0' or (file.startswith("rank_") and os.path.exists(dest_dir)):
                        pUtil.recursive_overwrite(path, dest_dir, ignore=self.ignore_files)
                except:
                    tolog("Failed to copy %s to %s: %s" % (path, dest_dir, traceback.format_exc()))
            for file in found_files:
                #if '.dump.' in file and not file.startswith(str(jobId)):
                #    continue
                path = found_files[file]
                dest_dir = os.path.join(job.workdir, file)
                try:
                    if "job.log.tgz." in file or "LOCKFILE" in file or "tarball_PandaJob" in file or "objectstore_info" in file:
                        continue
                    if file.endswith(".dump") or file.endswith(".dump.zipped") or file.startswith("metadata-") or "jobState-" in file\
                       or file.startswith("jobState-") or file.startswith("EventService_premerge")\
                       or file.startswith("Job_") or file.startswith("fileState-") or file.startswith("curl_updateJob_")\
                       or file.startswith("curl_updateEventRanges_")\
                       or file.startswith("surlDictionary") or file.startswith("jobMetrics-rank") or "event_status.dump" in file:
                        if str(jobId) in file:
                            pUtil.recursive_overwrite(path, dest_dir, ignore=self.ignore_files)
                    else:
                        pUtil.recursive_overwrite(path, dest_dir)
                except:
                    tolog("Failed to copy %s to %s: %s" % (path, dest_dir, traceback.format_exc()))

    def finishJobs(self):
        try:
            self.__hpcManager.finishJob()
        except:
            tolog(sys.exc_info()[1])
            tolog(sys.exc_info()[2])

        try:
            self.checkJobMetrics()
        except:
            tolog("RunHPCEvent failed: %s" % traceback.format_exc())

        try:
            if not self.__stageout_status == True:
                self.stageOutZipFiles()
        except:
            tolog("RunHPCEvent failed: %s" % traceback.format_exc())

        try:
            tolog("Copying Log files to Job working dir")
            self.copyLogFilesToJob()
        except:
            tolog("Failed to copy log files to job working dir: %s" % (traceback.format_exc()))

        # If payload leaves the input files, delete them explicitly
        firstJob = None
        for jobId in self.__jobs:
            try:
                if self.__firstJobId and (jobId == self.__firstJobId):
                    firstJob = self.__jobs[jobId]['job']
                    continue

                job = self.__jobs[jobId]['job']
                self.finishOneJob(job)
            except:
                tolog("Failed to finish one job %s: %s" % (job.jobId, traceback.format_exc()))
        if firstJob:
            try:
                self.finishOneJob(firstJob)
            except:
                tolog("Failed to finish the first job %s: %s" % (firstJob.jobId, traceback.format_exc()))
        time.sleep(1)

if __name__ == "__main__":

    tolog("Starting RunJobHpcEvent")

    if not os.environ.has_key('PilotHomeDir'):
        os.environ['PilotHomeDir'] = os.getcwd()

    # define a new parent group
    os.setpgrp()

    runJob = RunJobHpcEvent()
    try:
        runJob.setupHPCEvent()
        if runJob.getRecovery():
            # recovery job
            runJob.recoveryJobs()
            runJob.recoveryHPCManager()
            #runJob.runHPCEvent()
            runJob.runHPCEventJobsWithEventStager(useEventStager=False)
        else:
            runJob.setupHPCManager()
            runJob.getHPCEventJobs()
            runJob.stageInHPCJobs()
            runJob.startHPCJobs()
            #runJob.runHPCEvent()
            runJob.runHPCEventJobsWithEventStager(useEventStager=False)
    except:
        tolog("RunJobHpcEventException")
        tolog(traceback.format_exc())
        tolog(sys.exc_info()[1])
        tolog(sys.exc_info()[2])
    finally:
        runJob.finishJobs()
