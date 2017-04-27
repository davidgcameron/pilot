import re
import os

from pUtil import tolog, readpar, getExperiment


def extractSingularityOptions():
    """ Extract any singularity options from catchall """

    # e.g. catchall = "somestuff singularity_options=\'-B /etc/grid-security/certificates,/var/spool/slurmd,/cvmfs,/ceph/grid,/data0,/sys/fs/cgroup\'"
    catchall = readpar("catchall")
    pattern = re.compile(r" singularity\_options\=\'?\"?(.+)\'?\"?")
    found = re.findall(pattern, catchall)
    if len(found) > 0:
        singularity_options = found[0]
        if singularity_options.endswith("'"):
            singularity_options = singularity_options[:-1]
    else:
        singularity_options = ""

    return singularity_options

def getFileSystemRootPath(experiment):
    """ Return the proper file system root path (cvmfs) """

    e = getExperiment(experiment)
    return e.getCVMFSPath()

def getGridImageForSingularity(platform, experiment):
    """ Return the full path to the singularity grid image """

    image = platform + ".img"
    path = os.path.join(getFileSystemRootPath(experiment), "atlas.cern.ch/repo/images/singularity")
    return os.path.join(path, image)

def singularityWrapper(cmd, platform, experiment="ATLAS"):
    """ Prepend the given command with the singularity execution command """
    # E.g. cmd = /bin/bash hello_world.sh
    # -> singularity_command = singularity exec -B <bindmountsfromcatchall> <img> /bin/bash hello_world.sh
    # singularity exec -B <bindmountsfromcatchall>  /cvmfs/atlas.cern.ch/repo/images/singularity/x86_64-slc6.img <script> 

    # Get the singularity options from catchall field
    singularity_options = extractSingularityOptions()
    if singularity_options != "":
        # Get the image path
        image_path = getGridImageForSingularity(platform, experiment)

        # Does the image exist?
        if os.path.exists(image_path):
            # Prepend it to the given command
            cmd = "singularity exec " + singularity_options + " " + image_path + " " + cmd
        else:
            tolog("!!WARNING!!4444!! Singularity options found but image does not exist: %s" % (image_path))
    else:
        # Return the original command as it was
        pass

    return cmd

if __name__ == "__main__":

    cmd = "<some command>"
    platform = "x86_64-slc6"
    print singularityWrapper(cmd, platform)

