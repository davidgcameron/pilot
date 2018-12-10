import re
import os

from pUtil import tolog, readpar, getExperiment


def extractSingularityOptions():
    """ Extract any singularity options from catchall """

    # e.g. catchall = "somestuff singularity_options=\'-B /etc/grid-security/certificates,/var/spool/slurmd,/cvmfs,/ceph/grid,/data0,/sys/fs/cgroup\'"
    #catchall = "singularity_options=\'-B /etc/grid-security/certificates,/cvmfs,${workdir} --contain\'" #readpar("catchall")

    # ${workdir} should be there, otherwise the pilot cannot add the current workdir
    # if not there, add it

    # First try with reading new parameters from schedconfig
    container_options = readpar("container_options")
    if container_options == "":
        tolog("container_options either does not exist in queuedata or is empty, trying with catchall instead")
        catchall = readpar("catchall")
        #catchall = "singularity_options=\'-B /etc/grid-security/certificates,/cvmfs,${workdir} --contain\'"

        pattern = re.compile(r"singularity\_options\=\'?\"?(.+)\'?\"?")
        found = re.findall(pattern, catchall)
        if len(found) > 0:
            container_options = found[0]

    if container_options != "":
        if container_options.endswith("'") or container_options.endswith('"'):
            container_options = container_options[:-1]
        # add the workdir if missing
        if not "${workdir}" in container_options and " --contain" in container_options:
            container_options = container_options.replace(" --contain", ",${workdir} --contain")
            tolog("Note: added missing ${workdir} to singularity_options")

    return container_options

def getFileSystemRootPath(experiment):
    """ Return the proper file system root path (cvmfs) """

    e = getExperiment(experiment)
    return e.getCVMFSPath()

def extractPlatformAndOS(platform):
    """ Extract the platform and OS substring from platform """
    # platform = "x86_64-slc6-gcc48-opt"
    # return "x86_64-slc6"
    # In case of failure, return the full platform

    pattern = r"([A-Za-z0-9_-]+)-.+-.+"
    a = re.findall(re.compile(pattern), platform)

    if len(a) > 0:
        ret = a[0]
    else:
        tolog("!!WARNING!!7777!! Could not extract architecture and OS substring using pattern=%s from platform=%s (will use %s for image name)" % (pattern, platform, platform))
        ret = platform

    return ret

def getGridImageForSingularity(platform, experiment):
    """ Return the full path to the singularity grid image """

    if not platform or platform == "":
        platform = "x86_64-centos6"
        tolog("!!WARNING!!3333!! Using default platform=%s (cmtconfig not set)" % (platform))

    if "slc6" in platform:
        image = 'x86_64-centos6.img'
    else:
        arch_and_os = extractPlatformAndOS(platform)
        image = arch_and_os + ".img"
    tolog("Constructed image name %s from %s" % (image, platform))

    path = os.path.join(getFileSystemRootPath(experiment), "atlas.cern.ch/repo/containers/images/singularity")
    return os.path.join(path, image)

def getContainerName(user="pilot"):
    # E.g. container_type = 'singularity:pilot;docker:wrapper'
    # getContainerName(user='pilot') -> return 'singularity'

    container_name = ""
    container_type = readpar('container_type')

    if container_type != "" and user in container_type:
        try:
            container_names = container_type.split(';')
            for name in container_names:
                t = name.split(':')
                if user == t[1]:
                    container_name = t[0]
        except Exception as e:
            tolog("Failed to parse the container name: %s, %s" % (container_type, e))
    else:
        tolog("Container type not specified in queuedata")

    return container_name

def singularityWrapper(cmd, platform, workdir, experiment="ATLAS"):
    """ Prepend the given command with the singularity execution command """
    # E.g. cmd = /bin/bash hello_world.sh
    # -> singularity_command = singularity exec -B <bindmountsfromcatchall> <img> /bin/bash hello_world.sh
    # singularity exec -B <bindmountsfromcatchall>  /cvmfs/atlas.cern.ch/repo/images/singularity/x86_64-slc6.img <script> 

    # Should a container be used?
    container_name = getContainerName()
    if container_name == 'singularity':
        tolog("Singularity has been requested")

        # Get the singularity options
        singularity_options = extractSingularityOptions()
        if singularity_options != "":
            # Get the image path
            image_path = getGridImageForSingularity(platform, experiment)

            # Does the image exist?
            if os.path.exists(image_path):
                # Prepend it to the given command
                cmd = "export workdir=" + workdir + "; singularity exec " + singularity_options + " " + image_path + " /bin/bash -c \'cd $workdir;pwd;" + cmd.replace("\'","\\'").replace('\"','\\"') + "\'"
            else:
                tolog("!!WARNING!!4444!! Singularity options found but image does not exist: %s" % (image_path))
        else:
            # Return the original command as it was
            tolog("No singularity options found in container_options or catchall fields")

    tolog("Using command %s" % cmd)
    return cmd

if __name__ == "__main__":

    cmd = "<some command>"
    platform = "x86_64-slc6"
    print singularityWrapper(cmd, platform)

