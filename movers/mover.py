"""
  JobMover: top level mover to copy all Job files

  This functionality actually needs to be incorporated directly into RunJob,
  because it's just a workflow of RunJob
  but since RunJob at the moment is implemented as a Singleton
  it could be dangerous because of Job instance stored in the class object.
  :author: Alexey Anisenkov
"""

from subprocess import Popen, PIPE, STDOUT

from . import getSiteMover
from .trace_report import TraceReport

from FileStateClient import updateFileState, dumpFileStates
from PilotErrors import PilotException, PilotErrors

from pUtil import tolog

import time
import os
from random import shuffle


try:
    import json # python2.6
except ImportError:
    import simplejson as json


class JobMover(object):
    """
        Top level mover to copy Job files from/to storage.
    """

    job = None  # Job object
    si = None   # Site information

    MAX_STAGEIN_RETRY = 3
    MAX_STAGEOUT_RETRY = 10

    _stageoutretry = 2 # default value
    _stageinretry = 2  # devault value

    stagein_sleeptime = 10*60   # seconds, sleep time in case of stagein failure
    stageout_sleeptime = 10*60  # seconds, sleep time in case of stageout failure


    def __init__(self, job, si, **kwargs):

        self.job = job
        self.si = si

        self.workDir = kwargs.get('workDir', '')

        self.stageoutretry = kwargs.get('stageoutretry', self._stageoutretry)
        self.stageinretry = kwargs.get('stageinretry', self._stageinretry)

        self.protocols = {}
        self.ddmconf = {}
        self.trace_report = {}

        self.useTracingService = kwargs.get('useTracingService', self.si.getExperimentObject().useTracingService())


    def log(self, value): # quick stub

        #print value
        tolog(value)


    @property
    def stageoutretry(self):
        return self._stageoutretry


    @stageoutretry.setter
    def stageoutretry(self, value):
        if value >= self.MAX_STAGEOUT_RETRY or value < 1:
            ival = value
            value = JobMover._stageoutretry
            self.log("WARNING: Unreasonable number of stage-out tries: %d, reset to default value=%s" % (ival, value))
        self._stageoutretry = value


    @property
    def stageinretry(self):
        return self._stageinretry


    @stageoutretry.setter
    def stageinretry(self, value):
        if value >= self.MAX_STAGEIN_RETRY or value < 1:
            ival = value
            value = JobMover._stageinretry
            self.log("WARNING: Unreasonable number of stage-in tries: %d, reset to default value=%s" % (ival, value))
        self._stageinretry = value


    @classmethod
    def _prepare_input_ddm(self, ddm, localddms):
        """
            Sort and filter localddms for given preferred ddm endtry
            :return: list of ordered ddmendpoint names
        """
        # set preferred ddm as first source
        # move is_tape ddm to the end

        ret = [ ddm['name'] ]

        ddms, tapes = [], []
        for e in localddms:
            ddmendpoint = e['name']
            if ddmendpoint == ddm['name']:
                continue
            if e['is_tape']:
                tapes.append(ddmendpoint)
            else:
                ddms.append(ddmendpoint)

        # randomize input
        shuffle(ddms)

        # TODO: apply more sort rules here

        ddms = [ddm['name']] + ddms + tapes

        return ddms


    def resolve_replicas(self, files, protocols):

        # build list of local ddmendpoints: group by site
        # load ALL ddmconf
        self.ddmconf.update(self.si.resolveDDMConf([]))
        ddms = {}
        for ddm, dat in self.ddmconf.iteritems():
            ddms.setdefault(dat['site'], []).append(dat)

        for fdat in files:

            # build and order list of local ddms
            ddmdat = self.ddmconf.get(fdat.ddmendpoint)
            if not ddmdat:
                raise Exception("Failed to resolve ddmendpoint by name=%s send by Panda job, please check configuration. fdat=%s" % (fdat.ddmendpoint, fdat))
            if not ddmdat['site']:
                raise Exception("Failed to resolve site name of ddmendpoint=%s. please check ddm declaration: ddmconf=%s ... fdat=%s" % (fdat.ddmendpoint, ddmconf, fdat))
            localddms = ddms.get(ddmdat['site'])
            # sort/filter ddms (as possible input source)
            fdat.inputddms = self._prepare_input_ddm(ddmdat, localddms)

        # load replicats from Rucio
        from rucio.client import Client
        c = Client()

        dids = [dict(scope=e.scope, name=e.lfn) for e in files]
        schemes = ['srm', 'root', 'https', 'gsiftp']

        # Get the replica list
        try:
            replicas = c.list_replicas(dids, schemes=schemes)
        except Exception, e:
            raise PilotException("Failed to get replicas from Rucio: %s" % e, code=PilotErrors.ERR_FAILEDLFCGETREPS)

        files_lfn = dict(((e.lfn, e.scope), e) for e in files)

        for r in replicas:
            k = r['scope'], r['name']
            fdat = files_lfn.get(k)
            if not fdat: # not requested replica returned?
                continue
            fdat.replicas = [] # reset replicas list
            for ddm in fdat.inputddms:
                if ddm not in r['rses']: # skip not interesting rse
                    continue
                fdat.replicas.append((ddm, r['rses'][ddm]))

            if fdat.filesize != r['bytes']:
                self.log("WARNING: filesize value of input file=%s mismatched with info got from Rucio replica:  job.indata.filesize=%s, replica.filesize=%s, fdat=%s" % (fdat.lfn, fdat.filesize, r['bytes'], fdat))
            cc_ad = 'ad:%s' % r['adler32']
            cc_md = 'md:%s' % r['md5']
            if fdat.checksum not in [cc_ad, cc_md]:
                self.log("WARNING: checksum value of input file=%s mismatched with info got from Rucio replica:  job.indata.checksum=%s, replica.checksum=%s, fdat=%s" % (fdat.lfn, fdat.filesize, (cc_ad, cc_md), fdat))
            # update filesize & checksum info from Rucio?
            # TODO

        return files


    def stagein(self):

        activity = 'pr'

        pandaqueue = self.si.getQueueName() # FIX ME LATER
        protocols = self.protocols.setdefault(activity, self.si.resolvePandaProtocols(pandaqueue, activity)[pandaqueue])

        files = self.job.inData

        self.resolve_replicas(files, protocols)

        maxinputsize = self.getMaxInputSize()
        totalsize = reduce(lambda x, y: x + y.filesize, files, 0)

        transferred_files = []
        failed_transfers = []

        for dat in protocols:

            copytool, copysetup = dat.get('copytool'), dat.get('copysetup')

            try:
                sitemover = getSiteMover(copytool)(copysetup, workDir=self.job.workDir)
                sitemover.trace_report = self.trace_report
                sitemover.protocol = dat # ##
                sitemover.setup()
            except Exception, e:
                self.log('WARNING: Failed to get SiteMover: %s .. skipped .. try to check next available protocol, current protocol details=%s' % (e, dat))
                continue

            self.log("Copy command [stagein]: %s, sitemover=%s" % (copytool, sitemover))
            self.log("Copy setup   [stagein]: %s" % copysetup)

            ddmendpoint = dat.get('ddm')
            self.trace_report.update(protocol=copytool, localSite=ddmendpoint, remoteSite=ddmendpoint)

            self.log("Found N=%s files to be transferred, total_size=%.3f MB: %s" % (len(files), totalsize/1024., [e.lfn for e in files]))

            # verify file sizes and available space for stagein
            sitemover.check_availablespace(maxinputsize, files)

            for fdata in files:

                if fdata.status == 'transferred': # already transferred
                    continue

                updateFileState(fdata.lfn, self.workDir, self.job.jobId, mode="file_state", state="not_transferred", type="input")

                if fdat.is_directaccess(): # direct access mode, no transfer required
                    fdata.status = 'direct_access'
                    self.log("Direct access mode will be used for lfn=%s .. skip transfer the file" % fdata.lfn)
                    continue

                self.trace_report.update(catStart=time.time(), filename=fdata.lfn, guid=fdata.guid.replace('-', ''))
                self.trace_report.update(scope=fdata.scope, dataset=fdata.prodDBlocks)

                self.log("Preparing copy for lfn=%s using copytool=%s: mover=%s" % (fdata.lfn, copytool, sitemover))

                dumpFileStates(self.workDir, self.job.jobId)

                # loop over multple stage-in attempts
                for _attempt in xrange(1, self.stageintretry + 1):

                    if _attempt > 1: # if not first stage-out attempt, take a nap before next attempt
                        self.log(" -- Waiting %s seconds before next stage-in attempt for file=%s --" % (self.stagein_sleeptime, fdata.lfn))
                        time.sleep(self.stagein_sleeptime)

                    self.log("Put attempt %s/%s for filename=%s" % (_attempt, self.stageintretry, fdata.lfn))

                    try:
                        result = sitemover.get_data(fdata)
                        self.trace_report.update(url=result.get('surl')) ###
                        fdata.status = 'transferred' # mark as successful
                        break # transferred successfully
                    except PilotException, e:
                        result = e
                    except Exception, e:
                        result = PilotException("stageIn failed with error=%s" % e, code=PilotErrors.ERR_STAGEINFAILED)

                    self.log('WARNING: Error in copying file (attempt %s/%s): %s' % (_attempt, self.stageintretry, result))

                if not isinstance(result, Exception): # transferred successfully

                    # finalize and send trace report
                    self.trace_report.update(clientState='DONE', stateReason='OK', timeEnd=time.time())
                    self.sendTrace(self.trace_report)

                    updateFileState(fdata.lfn, self.workDir, self.job.jobId, mode="file_state", state="transferred", type="input")
                    dumpFileStates(self.workDir, self.job.jobId)

                    ## self.updateSURLDictionary(guid, surl, self.workDir, self.job.jobId) # FIX ME LATER

                    fdat = result.copy()
                    #fdat.update(lfn=lfn, pfn=pfn, guid=guid, surl=surl)
                    transferred_files.append(fdat)
                else:
                    failed_transfers.append(result)

        dumpFileStates(self.workDir, self.job.jobId)

        #self.log('transferred_files= %s' % transferred_files)
        self.log('Summary of transferred files:')
        for e in transferred_files:
            self.log(" -- %s" % e)

        if failed_transfers:
            self.log('Summary of failed transfers:')
            for e in failed_transfers:
                self.log(" -- %s" % e)

        self.log("stagein finished")

        return transferred_files, failed_transfers


    def put_outfiles(self, files):
        """
        Copy output files to dest SE
        :files: list of files to be moved
        :raise: an exception in case of errors
        """

        activity = 'pw'
        ddms = self.job.ddmEndPointOut

        if not ddms:
            raise PilotException("Output ddmendpoint list (job.ddmEndPointOut) is not set", code=PilotErrors.ERR_NOSTORAGE)

        return self.put_files(ddms, activity, files)


    def put_logfiles(self, files):
        """
        Copy log files to dest SE
        :files: list of files to be moved
        """

        activity = 'pl'
        ddms = self.job.ddmEndPointLog

        if not ddms:
            raise PilotException("Output ddmendpoint list (job.ddmEndPointLog) is not set", code=PilotErrors.ERR_NOSTORAGE)

        return self.put_files(ddms, activity, files)


    def put_files(self, ddmendpoints, activity, files):
        """
        Copy files to dest SE:
           main control function, it should care about alternative stageout and retry-policy for diffrent ddmenndpoints
        :ddmendpoint: list of DDMEndpoints where the files will be send (base DDMEndpoint SE + alternative SEs??)
        :return: list of entries (is_success, success_transfers, failed_transfers, exception) for each ddmendpoint
        :raise: PilotException in case of error
        """

        if not ddmendpoints:
            raise PilotException("Failed to put files: Output ddmendpoint list is not set", code=PilotErrors.ERR_NOSTORAGE)
        if not files:
            raise PilotException("Failed to put files: empty file list to be transferred")

        missing_ddms = set(ddmendpoints) - set(self.ddmconf)

        if missing_ddms:
            self.ddmconf.update(self.si.resolveDDMConf(missing_ddms))

        pandaqueue = self.si.getQueueName() # FIX ME LATER
        prot = self.protocols.setdefault(activity, self.si.resolvePandaProtocols(pandaqueue, activity)[pandaqueue])

        # group by ddmendpoint
        ddmprot = {}
        for e in prot:
            ddmprot.setdefault(e['ddm'], []).append(e)

        output = []

        for ddm in ddmendpoints:
            protocols = ddmprot.get(ddm)
            if not protocols:
                self.log('Failed to resolve protocols data for ddmendpoint=%s and activity=%s.. skipped processing..' % (ddm, activity))
                continue

            success_transfers, failed_transfers = [], []

            try:
                success_transfers, failed_transfers = self.do_put_files(ddm, protocols, files)
                is_success = len(success_transfers) == len(files)
                output.append((is_success, success_transfers, failed_transfers, None))

                if is_success:
                    # NO additional transfers to another next DDMEndpoint/SE ?? .. fix me later if need
                    break

            #except PilotException, e:
            #    self.log('put_files: caught exception: %s' % e)
            except Exception, e:
                self.log('put_files: caught exception: %s' % e)
                # is_success, success_transfers, failed_transfers, exception
                import traceback
                self.log(traceback.format_exc())
                output.append((False, [], [], e))

            ### TODO: implement proper logic of put-policy: how to handle alternative stage out (processing of next DDMEndpoint)..

            self.log('put_files(): Failed to put files to ddmendpoint=%s .. successfully transferred files=%s/%s, failures=%s: will try next ddmendpoint from the list ..' % (ddm, len(success_transfers), len(files), len(failed_transfers)))

        # check transfer status: if any of DDMs were successfull
        n_success = reduce(lambda x, y: x + y[0], output, False)

        if not n_success:  # failed to put file
            self.log('put_outfiles failed')
        else:
            self.log("Put successful")


        return output


    def do_put_files(self, ddmendpoint, protocols, files):
        """
        Copy files to dest SE
        :ddmendpoint: DDMEndpoint name used to store files
        :return: (list of transferred_files details, list of failed_transfers details)
        :raise: PilotException in case of error
        """

        self.log('Prepare to copy files=%s to ddmendpoint=%s using protocols data=%s' % (files, ddmendpoint, protocols))
        self.log("Number of stage-out tries: %s" % self.stageoutretry)

        # get SURL for Panda calback registration
        # resolve from special protocol activity=SE # fix me later to proper name of activitiy=SURL (panda SURL, at the moment only 2-letter name is allowed on AGIS side)

        surl_prot = [dict(se=e[0], path=e[2]) for e in sorted(self.ddmconf.get(ddmendpoint, {}).get('aprotocols', {}).get('SE', []), key=lambda x: x[1])]

        if not surl_prot:
            self.log('FAILED to resolve default SURL path for ddmendpoint=%s' % ddmendpoint)
            return [], []

        surl_prot = surl_prot[0] # take first
        self.log("SURL protocol to be used: %s" % surl_prot)


        self.trace_report.update(localSite=ddmendpoint, remoteSite=ddmendpoint)

        transferred_files = []
        failed_transfers = []

        for dat in protocols:

            copytool, copysetup = dat.get('copytool'), dat.get('copysetup')

            try:
                sitemover = getSiteMover(copytool)(copysetup)
                sitemover.setup()
            except Exception, e:
                self.log('WARNING: Failed to get SiteMover: %s .. skipped .. try to check next available protocol, current protocol details=%s' % (e, dat))
                continue

            self.log("Copy command: %s, sitemover=%s" % (copytool, sitemover))
            self.log("Copy setup: %s" % copysetup)

            self.trace_report.update(protocol=copytool)
            sitemover.trace_report = self.trace_report

            se, se_path = dat.get('se', ''), dat.get('path', '')

            self.log("Found N=%s files to be transferred: %s" % (len(files), [e.get('pfn') for e in files]))

            for fdata in files:
                scope, lfn, pfn = fdata.get('scope', ''), fdata.get('lfn'), fdata.get('pfn')
                guid = fdata.get('guid', '')

                surl = sitemover.getSURL(surl_prot.get('se'), surl_prot.get('path'), scope, lfn, self.job) # job is passing here for possible JOB specific processing
                turl = sitemover.getSURL(se, se_path, scope, lfn, self.job) # job is passing here for possible JOB specific processing

                self.trace_report.update(scope=scope, dataset=fdata.get('dsname_report'), url=surl)
                self.trace_report.update(catStart=time.time(), filename=lfn, guid=guid.replace('-', ''))

                self.log("Preparing copy for pfn=%s to ddmendpoint=%s using copytool=%s: mover=%s" % (pfn, ddmendpoint, copytool, sitemover))
                self.log("lfn=%s: SURL=%s" % (lfn, surl))
                self.log("TURL=%s" % turl)

                if not os.path.isfile(pfn) or not os.access(pfn, os.R_OK):
                    error = "Erron: input pfn file is not exist: %s" % pfn
                    self.log(error)
                    raise PilotException(error, code=PilotErrors.ERR_MISSINGOUTPUTFILE, state="FILE_INFO_FAIL")

                filename = os.path.basename(pfn)

                # update the current file state
                updateFileState(filename, self.workDir, self.job.jobId, mode="file_state", state="not_transferred")
                dumpFileStates(self.workDir, self.job.jobId)

                # loop over multple stage-out attempts
                for _attempt in xrange(1, self.stageoutretry + 1):

                    if _attempt > 1: # if not first stage-out attempt, take a nap before next attempt
                        self.log(" -- Waiting %d seconds before next stage-out attempt for file=%s --" % (self.stageout_sleeptime, filename))
                        time.sleep(self.stageout_sleeptime)

                    self.log("Put attempt %d/%d for filename=%s" % (_attempt, self.stageoutretry, filename))

                    try:
                        result = sitemover.stageOut(pfn, turl)
                        break # transferred successfully
                    except PilotException, e:
                        result = e
                    except Exception, e:
                        result = PilotException("stageOut failed with error=%s" % e, code=PilotErrors.ERR_STAGEOUTFAILED)

                    self.log('WARNING: Error in copying file (attempt %s): %s' % (_attempt, result))

                if not isinstance(result, Exception): # transferred successfully

                    # finalize and send trace report
                    self.trace_report.update(clientState='DONE', stateReason='OK', timeEnd=time.time())
                    self.sendTrace(self.trace_report)

                    updateFileState(filename, self.workDir, self.job.jobId, mode="file_state", state="transferred")
                    dumpFileStates(self.workDir, self.job.jobId)

                    self.updateSURLDictionary(guid, surl, self.workDir, self.job.jobId) # FIX ME LATER

                    fdat = result.copy()
                    fdat.update(lfn=lfn, pfn=pfn, guid=guid, surl=surl)
                    transferred_files.append(fdat)
                else:
                    failed_transfers.append(result)


        dumpFileStates(self.workDir, self.job.jobId)

        self.log('transferred_files= %s' % transferred_files)

        if failed_transfers:
            self.log('Summary of failed transfers:')
            for e in failed_transfers:
                self.log(" -- %s" % e)

        return transferred_files, failed_transfers


    @classmethod
    def updateSURLDictionary(self, guid, surl, directory, jobId): # OLD functuonality: FIX ME LATER: avoid using intermediate buffer
        """
            add the guid and surl to the surl dictionary
        """

        # temporary quick workaround: TO BE properly implemented later
        # the data should be passed directly instead of using intermediate JSON/CPICKLE file buffers
        #

        from SiteMover import SiteMover
        return SiteMover.updateSURLDictionary(guid, surl, directory, jobId)

    @classmethod
    def getMaxInputSize(self):

        # Get a proper maxinputsize from schedconfig/default
        # quick stab: old implementation, fix me later

        from SiteMover import SiteMover
        return SiteMover.getMaxInputSize()


    def sendTrace(self, report):
        """
            Go straight to the tracing server and post the instrumentation dictionary
            :return: True in case the report has been successfully sent
        """

        if not self.useTracingService:
            self.log("Experiment is not using Tracing service. skip sending tracing report")
            return False

        url = 'https://rucio-lb-prod.cern.ch/traces/'

        self.log("Tracing server: %s" % url)
        self.log("Sending tracing report: %s" % report)

        try:
            # take care of the encoding
            #data = urlencode({'API':'0_3_0', 'operation':'addReport', 'report':report})

            data = json.dumps(report)
            data = data.replace('"','\\"') # escape quote-char

            sslCertificate = self.si.getSSLCertificate()

            cmd = 'curl --connect-timeout 20 --max-time 120 --cacert %s -v -k -d "%s" %s' % (sslCertificate, data, url)
            self.log("Executing command: %s" % cmd)

            c = Popen(cmd, stdout=PIPE, stderr=STDOUT, shell=True)
            output = c.communicate()[0]
            if c.returncode:
                raise Exception(output)
        except Exception, e:
            self.log('WARNING: FAILED to send tracing report: %s' % e)
            return False

        self.log("Tracing report successfully sent to %s" % url)
        return True
