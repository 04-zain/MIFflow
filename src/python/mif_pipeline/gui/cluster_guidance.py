"""Preparation guidance for cluster desktop and streaming analysis."""

SOFTWARE_REQUIREMENTS = """Analysis software on the cluster
Install the same pipeline release using Python 3.12, QuPath 0.7.0, Java 21 and Bio-Formats 8.5.0. Paired-image alignment uses Warpy 0.5.0, and Elastix and Transformix 5.3.1. Cell segmentation uses InstanSeg 0.1.7, its model and the DJL/PyTorch runtime. KRONOS uses its approved model package and PyTorch environment.
"""

CLUSTER_DESKTOP_SETUP = SOFTWARE_REQUIREMENTS + """
Open the application
Start an allocated cluster desktop using your institution's service. Open the pipeline and QuPath there, then complete Software setup.

Create the analysis
Choose New analysis and select This computer in Computing. Add your images on Samples, review Plan and start Run.

Reviews and results
Complete forms and annotations in that desktop. The analysis and its results remain on the cluster.
"""

CLUSTER_SETUP = SOFTWARE_REQUIREMENTS + """
On this computer
Open the pipeline and select Cluster streaming before adding samples. Complete local QuPath setup for image viewing. Complete paired-image alignment setup when the analysis includes a paired image.

SSH connection
Use user@host or a saved SSH connection name. Configure that connection to sign in automatically, using your institution's SSH key or SSH agent. Connect once in a terminal to verify the host. 

Cluster software
Enter the installed mif-batch program; command -v mif-batch reports its path after environment activation. If the cluster environment is not configured by default, supply a shell script containing your activation commands. This script prepares the environment at the start of each Slurm job and before mif-batch commands. List any additional module names separately; they are loaded after the script.

Image access and storage
Connect the cluster folder read-only using your SSHFS client. In Image access, enter the connected cluster path and the mounted folder or drive supplied by the client. This connection must include your images and the location chosen for saving the analysis.

Cluster resources
The cluster needs Slurm and an allocation with access to the selected storage and software. Choose the processor cores, memory, time and GPUs needed by the analysis. Live detector-model authoring uses the cluster desktop.
"""

CLUSTER_DESKTOP = """Open the pipeline and QuPath in an allocated cluster desktop. Create the analysis there and choose This computer in Computing."""

