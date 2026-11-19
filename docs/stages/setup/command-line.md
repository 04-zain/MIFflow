# Command-line guide

<a id="reference-command-line"></a>
<a id="reference-command-line--command-line-interface"></a>

Use `mif-batch` on the processing computer to start an analysis, check its progress and submit review decisions. To submit jobs to Slurm from your own computer, use `mif-remote`. Both commands read the analysis manifest prepared in the desktop. The [installation guide](../installation-and-build/software.md#install-a-shared-wheel) explains how to install the commands on a processing host.

A wheel or source installation places the launchers in the Python environment where you installed MIFflow. If the environment is called `.venv`, use either column below from its parent directory. Activating it lets you type a command by name for the rest of the session. To launch just one program by its path, substitute its name for `COMMAND`, for example `mif-desktop`. The arguments in the following sections apply to Windows and Ubuntu.

| Operating system | Activate the environment | Run a command without activating it |
|---|---|---|
| Ubuntu | `source .venv/bin/activate` | `.venv/bin/COMMAND` |
| Windows PowerShell | `.\.venv\Scripts\Activate.ps1` | `.\.venv\Scripts\COMMAND.exe` |

| Command | Purpose |
|---|---|
| `mif-desktop` | Open the graphical application to prepare samples, select methods and review results. Install the `desktop` extra to use it. |
| `mif-batch` | Prepare the processing host, run an analysis and submit decisions for stages waiting for review. |
| `mif-remote` | Submit a batch run to Slurm over SSH and check the job from another computer. |
| `mif-kernel-worker` | Run individual calculations sent by a MIFflow process. |

## Use the batch commands

If this is the first analysis on the processing host, begin with [Software setup](#software-setup). If the software is ready and the sample list has been saved, go to [Start a prepared analysis](#start-a-prepared-analysis). To return to an analysis that has already started, [check its progress](#check-progress-and-continue-processing).

Replace uppercase placeholders with your own paths or identifiers. Options for the whole run go between `mif-batch` and the operation name. Options for one operation follow its name. Use `mif-batch --help` to list the operations or `mif-batch COMMAND --help` for the arguments to one of them.

<a id="reference-command-line--software-setup"></a>
## Software setup

Prepare the software on the computer that will read the images and run the analysis. For a cluster job, the processing node must be able to open the executable, model and image paths selected here. Run setup under the same account as the analysis so it can use the saved installations. [Software setup in the desktop](../installation-and-build/software.md#open-software-setup)

### Prepare the workspace and connect QuPath

Run `mif-batch setup-workspace` once under the processing account to initialize its workspace. Running it again checks the existing workspace.

QuPath loads our image reader and review tools from an extension folder prepared for MIFflow. Supply the path to QuPath and an existing parent directory for this folder.

```text
mif-batch --qupath EXECUTABLE setup-qupath-profile DIRECTORY --consent-download
```

The result includes `qupath_profile_manifest`, the path to the file that identifies the prepared QuPath installation and its extensions. For subsequent commands, place `--qupath EXECUTABLE --qupath-profile MANIFEST` before the operation name. If several commands will use the same installation, set `MIF_QUPATH_EXECUTABLE` and `MIF_QUPATH_PROFILE` once in the terminal or job environment. [How QuPath loads the extensions](../installation-and-build/native-extension.md#how-qupath-loads-the-extension)

For a host that needs the native PyTorch CPU libraries for QuPath, run `mif-batch setup-cpu DIRECTORY --consent-download`. Supply an absolute destination. This installs the platform’s CPU libraries and Java bridge for models running inside QuPath.

### Connect Java and Bio-Formats

Bio-Formats runs in Java to read the original image files. First use `setup-image inspect java` to check the Java runtime, then `setup-image inspect bioformats` to check the reader with that runtime. If either is missing, select an existing copy or install the supported version. In the commands below, replace `COMPONENT` with `java` or `bioformats`.

| Arguments after `mif-batch` | When to use them |
|---|---|
| `setup-image inspect COMPONENT` | Check the selected or managed installation. |
| `setup-image select COMPONENT DIRECTORY` | Check an existing Java home or Bio-Formats root. |
| `setup-image install COMPONENT --consent-download` | Install the supported release. Add `--managed-root DIRECTORY` to specify where managed installations are kept. |

When the response includes `next_command_option` and `next_command_path`, use them together before the next command. For instance, after selecting a Java installation, run `mif-batch --java-home DIRECTORY setup-image inspect bioformats` to check the reader with that runtime.

### Install alignment and segmentation software

Choose the rows for the methods used in the analysis. For InstanSeg and Warpy, supply `--qupath EXECUTABLE` before the command. Include `--qupath-profile MANIFEST` when using the profile prepared above.

| Component | Arguments after `mif-batch` and its global options |
|---|---|
| Warpy extension | `setup-warpy inspect` or `setup-warpy install --consent-download` |
| Elastix and Transformix executables | `setup-elastix inspect`, `setup-elastix select DIRECTORY` or `setup-elastix install --consent-download`. To change the managed location, pass `--target DIRECTORY`. |
| InstanSeg extension and model | `setup-instanseg inspect COMPONENT` or `setup-instanseg install COMPONENT --consent-download`. Use `extension` or `model` for `COMPONENT`. Use `--extension-directory DIRECTORY` to specify an extension folder. |
| Cellpose or StarDist model | `setup-segmentation METHOD inspect` or `setup-segmentation METHOD install --consent-download`. Use `cellpose` or `stardist` for `METHOD`. |

To keep a Cellpose or StarDist model in a custom location, add `--directory DIRECTORY` to its setup command. Set `MIF_CELLPOSE_MODEL_DIRECTORY` or `MIF_STARDIST_MODEL_DIRECTORY` to the same absolute directory when launching the desktop or worker.

### Select the Nimbus checkpoint

Install the [Nimbus scoring software](../installation-and-build/software.md#nimbus-for-source-installations) before using the model. The following commands manage the V1 checkpoint.

| Arguments after `mif-batch` | Purpose |
|---|---|
| `setup-nimbus inspect [--target FILE]` | Check the checkpoint at its default location or the specified absolute filename. |
| `setup-nimbus install --consent-download [--target FILE]` | Download and verify the checkpoint. |
| `setup-nimbus select FILE` | Verify an existing checkpoint. The response reports whether its path was saved in the application preferences. |

If `preference_saved` is false after selection, use the returned `next_step` to enter the path under **Nimbus model checkpoint** in marker setup.

### Other processing paths

Use `--java-home` and `--bioformats-root` when specifying an external Java installation and image reader. A processing host that supplies its own QuPath libraries can pass `--instanseg-jar`, `--djl-jar`, `--pytorch-engine-jar`, `--cpu-native-root` and `--qupath-cache-root`. Supply all five as absolute paths. For an analysis that sends selected calculations to another host, `--selective-compute-profile FILE` selects the saved profile for those calculations.

<a id="prepare-and-execute"></a>
<a id="reference-command-line--prepare-and-execute"></a>
## Start a prepared analysis

First [save the samples and computing settings in the desktop](analysis.md#save-and-inspect-plan). This creates `analysis.manifest.json`, which identifies the input images, the analysis ID (`batch_id`) and the parent directory for its results (`output_root`). Pass the manifest path to the start command. To check or resume that run later, use the output root and batch ID from the same manifest.

```text
mif-batch validate MANIFEST
mif-batch plan MANIFEST
mif-batch run MANIFEST
```

1. **Check the inputs.** `validate` reads the manifest. Its `enabled_sample_ids` list identifies the samples included in this run.
2. **Inspect the plan.** `plan` shows which stages will run for each sample and which depend on a result shared by several samples.
3. **Start processing.** `run` executes the plan. When a sample reaches a review, processing can continue on other samples whose inputs are ready. The command returns a JSON report when execution stops. Check its `outcome` and `pending_interaction_ids` to see whether the run completed or is waiting for a decision.

In cluster streaming, the desktop prepares the same analysis by sending its sample and computing choices to `mif-batch prepare-stdin` as JSON. The response identifies the new manifest, batch ID and sample count. When reopening the analysis, `mif-batch describe-analysis ANALYSIS_FOLDER` retrieves its saved setup from the cluster. [Cluster setup document](../../../src/python/mif_pipeline/orchestration/setup/cluster_setup.py)

## Check progress and continue processing

After starting a run, use the output root and batch ID from its manifest to check progress.

```text
mif-batch status OUTPUT_ROOT BATCH_ID
```

Read `pending_interaction_ids` to see whether a stage is waiting for a decision. If it is, [retrieve the request and submit the answer](#submit-a-waiting-review). Once a review is accepted, or when you return to a paused analysis, run `mif-batch resume OUTPUT_ROOT BATCH_ID`. This reopens the plan, checks the software needed for the remaining stages and starts ready work.

After an interruption, check `resume_disposition` before starting another process. If the run is idle but still recorded as active and continuation is offered, use `mif-batch continue OUTPUT_ROOT BATCH_ID --expected-head-sha256 HEAD`. Replace `HEAD` with the `state_head_sha256` returned by `status`. This identifies the exact saved state to continue. If an operation recorded a failure, read its error and [list the available retries](#retry-a-failed-or-interrupted-operation). For a completed run, open its [results](../results/index.md).

To check the capabilities of the processing host itself, use `mif-batch runtime-capabilities OUTPUT_ROOT BATCH_ID`. It reports the available processing devices without launching QuPath.

When scripting a run, read the JSON report on standard output. A run that stops for a review can exit successfully with `outcome` set to `needs_interaction`. A failed or incomplete run returns exit status 1, as does a rejected review response. Argument and execution errors return status 2, with diagnostics on standard error.

<a id="reviews-and-retry"></a>
<a id="reference-command-line--reviews-and-retry"></a>
## Submit a waiting review

### Find the request and choose its targets

Run `mif-batch interactions OUTPUT_ROOT BATCH_ID` to list the decisions holding up processing. Each group identifies the stage and reason for the review (`stage` and `interaction_reason`), the samples or other targets it affects (`targets`) and the actions you can take (`allowed_actions`). Inspect the targets before choosing a shared response. One group may include several samples that need different decisions.

Use `mif-batch review-page OUTPUT_ROOT BATCH_ID` when you need the records shown in the desktop review or the location of its QuPath project. Both listing commands accept `--domain ID` and `--reason REASON` to narrow the results. They return up to 50 entries per page by default. To read the next page, pass the returned `next_offset` to `--offset`, or set a different page size with `--limit`.

### Send the decision and resume

Copy `group_id` and `group_sha256` from the listing entry for the review you want to answer. To check that the analysis still has the inputs you reviewed, we also need the listing’s top-level `state_head_sha256`. Enter it as `expected_state_head_sha256` in the response.

In `selection`, specify the target IDs to receive this answer. If the group permits it, you can select validation domains with `{"validation_domain_ids": ["ID"]}` or all compatible targets with `{"all_compatible": true}`. Choose an offered action and fill in its requested fields under `response`. An action that requires no fields takes an empty object.

The example shows the JSON file for a single target. [Submit the same decision from Python](python-api.md#open-an-analysis-service-and-submit-a-review)

```json
{
  "group_id": "<group_id>",
  "group_sha256": "<group_sha256>",
  "expected_state_head_sha256": "<state_head_sha256>",
  "selection": {"target_ids": ["<target_id>"]},
  "action": "<allowed_action>",
  "response": {}
}
```

Submit the file with `mif-batch respond OUTPUT_ROOT BATCH_ID RESPONSE_FILE`. A program that already has the response as JSON can send it to `mif-batch respond-stdin OUTPUT_ROOT BATCH_ID` on standard input. Both commands accept up to 1 MiB of UTF-8 JSON.

Check the returned status for each selected target. `accepted_for_retry` means the stage can run again with the answer you supplied. Use `resume` to continue. If a response was rejected because the analysis changed, list the requests again and review the current targets and choices before submitting a replacement.

## Retry a failed or interrupted operation

Run `mif-batch list-retries OUTPUT_ROOT BATCH_ID`. The result identifies the operations eligible for retry and the saved state to which those choices apply. Copy the node ID, state checksum and failed-attempt or pending-authorization checksum from the same listing.

```text
mif-batch retry OUTPUT_ROOT BATCH_ID --node-id NODE --expected-head-sha256 HEAD --terminal-record-sha256 RECORD
```

For a listed pending authorization, replace `--terminal-record-sha256 RECORD` with `--pending-authorization-sha256 RECORD`. If the listing has no eligible entry, check `status` for the available action. [Troubleshoot a stopped step](../../troubleshooting.md#resume-a-stopped-step)

## Change a completed stage

When changing the settings of a completed stage, first check which of its results and later stages will have to be recalculated. Each command below takes `OUTPUT_ROOT BATCH_ID` after its name. Use the same proposed settings for the preview and the revision so the changes you apply match the ones you reviewed.

| Command | Arguments after `OUTPUT_ROOT BATCH_ID` and input |
|---|---|
| `stage-options` | Without additional options, list the stages that can be revised. Add `--node-id NODE --expected-head-sha256 HEAD` for the settings of one stage. For an image-inspection or registration change, `--source-image FILE` inspects the replacement image. |
| `preview-stage-revision` | Pass `--node-id NODE --expected-head-sha256 HEAD` and supply the proposed settings as JSON on standard input. Inspect the affected results before applying the change. |
| `revise-stage` | Pass the same node, state checksum and settings to apply the revision. |

## Transfer reviews between the desktop and a cluster

In [cluster streaming](computing.md#cluster-streaming), the desktop can retrieve a waiting review from the processing host, open its material locally and return the edits. The commands below are the individual transfers used by the desktop. Use them directly when automating the same exchange.

To open a cluster review, use the request ID to identify which annotations, landmarks or figures the desktop should retrieve. The state checksum identifies the version being reviewed. To return edits, first stage the edited files or settings on the cluster. Use the submission ID returned by that command to import the edits into the same review. In the table below, each command takes `OUTPUT_ROOT BATCH_ID` before the listed arguments.

| Task | Command and remaining arguments | What it does |
|---|---|---|
| Open annotations | `annotations-export REQUEST_ID EXPECTED_HEAD` | Export the requested annotation material. Optional `--entry-id`, `--preview-id` and `--reference-role JSON` identify an entry, a preview or the reference labels. |
| Prepare an annotation preview | `stage-annotation-preview` | Read the preview request from standard input and prepare it for the annotation session. |
| Return edited annotations | `stage-annotations`, then `annotations-import SUBMISSION_ID` | Receive the annotation submission on standard input, then apply the staged edits to the current review. |
| Open landmarks | `stage-landmark-review`, then `landmark-export SUBMISSION_ID` | Stage the landmark review request from standard input, then retrieve the landmarks for editing. |
| Return edited landmarks | `stage-landmarks`, then `landmark-import SUBMISSION_ID` | Stage the returned landmarks from standard input, then submit them to the waiting review. |
| Apply paired-image alignment | `align RESPONSE_FILE SETTINGS_FILE` | Submit a review response with the alignment settings from a second JSON file. |
| Apply staged alignment | `stage-alignment`, then `align-staged SUBMISSION_ID` | Receive alignment material through standard input, then apply the staged submission. |
| Retrieve review figures | `review-figures REQUEST_ID EXPECTED_HEAD` | Retrieve figures for a current review. Use `--umap-settings JSON` and `--attribute NAME` for a UMAP request. The default attribute is `cluster_id`. |

## Submit and monitor a Slurm job

Use `mif-remote PROFILE OPERATION` with the JSON profile for the SSH connection and Slurm allocation. The profile identifies the analysis and tells Slurm which program and resources it needs. [Set up cluster streaming](computing.md#cluster-streaming)

Set `host` to the SSH alias or `user@host` used to reach the cluster. Give the absolute cluster paths to the installed `mif-batch` program (`batch_executable`), the analysis manifest (`manifest`), the results directory (`output_root`) and the directory for job files (`working_directory`). Use the `output_root` and `batch_id` recorded in the manifest so subsequent status and resume commands open the same analysis. The optional `cpus`, `memory_gib`, `hours` and `gpus` fields set the Slurm allocation.

Run `mif-remote PROFILE preview` to inspect the command and Slurm script, then submit the job with `mif-remote PROFILE run`. Use the returned job ID with `status --job-id ID` to check whether Slurm has started the job. Use `analysis-status` to find which stage is processing or waiting for review.

| Operation | Result |
|---|---|
| `preview` | Print the remote command and submission script for inspection. |
| `run` | Submit a job to start the prepared analysis. |
| `resume` | Submit a job to resume the saved analysis. |
| `analysis-status` | Read the progress of the MIFflow analysis. |
| `status --job-id ID` | Check the Slurm job returned by submission. |
| `cancel --job-id ID` | Ask Slurm to cancel that job. |

<a id="reference-command-line--numerical-processing-program"></a>
<a id="numerical-processing-program"></a>
## Check the calculation worker

Run `mif-kernel-worker --probe` on the processing host to report the worker version and supported request format. For calculations, the client starts `mif-kernel-worker --stdio`, sends the inputs through standard input and reads the result from standard output. It supplies the request ID, message lengths and checksums as part of that exchange.

[Run and review an analysis](../run/index.md)
