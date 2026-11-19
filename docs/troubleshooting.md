# Troubleshooting

If an analysis has stopped, select the affected step on **Run** and open **Details**. The failed operation and its last saved result tell you where to resume. If processing has yet to start, begin with the failed check on **Plan**.

[Startup and software](#startup-and-software) · [Images and calibration](#images-and-calibration) · [Forms and QuPath](#reviews-and-annotations) · [Interrupted processing](#resume-a-stopped-step) · [Cluster submission](#cluster-submission-outcome-is-uncertain)

## Startup and software

### A required software check fails

Open **Details** on the failed Plan row to identify the executable, library or model that could not be loaded. In **Software setup**, use the row for that component to select an existing installation or install the supported version. Close QuPath before replacing an extension. If two versions were found, check the paths in the error to identify the competing copies.

If an InstanSeg check fails, open **Cell segmentation** to identify which component needs attention. QuPath needs the extension to start segmentation, the model directory to find the trained network and the native PyTorch libraries to run it. Use the separate Cellpose-SAM or StarDist checks if you selected one of those methods.

If you change Java or Bio-Formats after the image reader has started, restart the desktop so the next Java session loads the selected files. [Software setup](stages/installation-and-build/software.md#open-software-setup)

### The desktop cannot connect to WSLg

If the terminal reports `Failed to create wl_display (No such file or directory)`, Qt could not open the Wayland display socket. In WSL, `XDG_RUNTIME_DIR` may point to `/run/user/1000` even though WSLg placed the socket in `/mnt/wslg/runtime-dir`. Check for WSL updates from Windows with `wsl --update`. [Microsoft WSL issue](https://github.com/microsoft/WSL/issues/11261)

If `/mnt/wslg/runtime-dir/wayland-0` exists, launch the desktop using that socket. This selects Wayland even if Qt previously reported a missing X11 `xcb` library.

```bash
WAYLAND_DISPLAY=/mnt/wslg/runtime-dir/wayland-0 \
QT_QPA_PLATFORM=wayland \
.venv/bin/mif-desktop
```

## Images and calibration

### Source file cannot be inspected

Check that the processing computer can read the complete file at the path shown in **Details**. On **Samples**, inspect the file and select the series containing the tissue. If the file became unavailable after processing started, restore access and choose **Retry step…** when offered. To use a different file or series, open **Image inspection → Change source image…** and review which results will need recalculation. Keep the reader error if the original file still cannot be opened. [Replace a source image](stages/run/index.md#replace-a-source-image-or-select-another-series)

<a id="image-state-and-calibration"></a>
<a id="channel-or-acquisition-information-conflicts"></a>
### Conflicting channel identities or imaging settings

First determine whether the source contains measured spectral bands or separated components. For spectral bands, compare the reported channel order and intensity scale with those of the reference library. For separated components, confirm the component identities in the export record and select the separated-image route. Preparation then preserves their intensities in OME-Zarr.

If the conflict concerns intensity correction, check which operations the image metadata records. Recorded offset subtraction is taken into account, and fully calibrated values that already include exposure normalization can be used directly. Full correction of raw intensities needs the stored integer range and the required detector and exposure settings. If metadata is incomplete, open the spectral-preparation review to supply the missing values or choose exposure-only correction when appropriate. [Correction routes and required settings](stages/images/processing.md#complete-acquisition-information)

## Reviews and annotations

### A form still asks for input

Open the review for the sample or group named on Run. Check the message beside each highlighted field. A change to the method or image can change which fields it requires. For an annotation request, check that the draft has returned to the matching review and that the page shows the intended references before accepting them.

### Return a saved QuPath draft

Save the draft in its original analysis folder and close the editing session. Return to the review that opened it and use its import or acceptance action. In cluster streaming, restore the connection and confirm the local folder is mapped to the intended analysis on the cluster. If the image or segmentation changed after the draft was drawn, reopen the review and check which image and cells it now expects. [Return a QuPath annotation draft](stages/run/index.md#return-a-qupath-annotation-draft)

### Predictions are absent or incomplete

Open the accepted model for the selected image and make its prediction overlay visible. If adjusting the opacity reveals predictions in only part of the tissue, compare the edge of the overlay with the area chosen for classification. For example, when a secondary classifier is restricted to selected primary tissue classes, its predictions end at those class boundaries.

For a KRONOS fit, check that the reference regions contain complete training patches. To apply the model, check the selected model package, marker order and physical patch scale against the fit. [KRONOS image inputs](stages/tissue-classification/kronos.md)

<a id="a-stopped-step-has-no-retry-action"></a>
## Resume a stopped step

Read **Details** before repeating the calculation. If a file, software installation or storage device became unavailable, restore it and refresh Run. The action shown depends on how far the previous attempt progressed.

| Action on Run | What to do |
|---|---|
| **Continue saved work** | Reopen the unfinished attempt. For tasks such as marker fitting and clustering, we save the inputs and any review response so processing can resume with the decision you already submitted. |
| **Retry step…** | Start another attempt at the failed operation with its saved inputs and settings. |
| **Restore accepted regions** | After an interrupted first segmentation, restore the accepted tissue and analysis regions before reviewing the segmentation settings and running it again. |
| **Edit spectral references…** or **Change…** | Revise the inputs or settings responsible for the failure. Review which later results depend on that change before applying it. [Revise an analysis](stages/run/index.md#change-a-completed-or-stopped-step) |

If no recovery action appears, establish whether the original process is still running. This is especially important after losing a cluster connection. For a process that wrote to QuPath, we also need to identify the image state it saved before allowing another write. Keep the project, draft and execution records together. If the process has ended and refreshing Run still offers no action, include the step details in a [problem report](#reporting-a-problem).

If segmentation completed with no cells, inspect the staining and the selected tissue area before changing the segmentation settings. If measurements report a different cell population from the one requested, check which segmentation and edits are currently accepted.

### Working memory or device memory is exhausted

**Waiting for memory** means a task is ready but has yet to start. Compare the requirement shown on Run with the available RAM on the processing computer or the memory assigned to the Slurm job. Close other memory-intensive processes or request a larger allocation, then continue the saved analysis.

If a task failed during GPU inference, check device memory separately. The computer needs RAM for image reads, the Java heap and native arrays. The GPU needs memory for model weights, input tiles and intermediate results. For InstanSeg, open **Cell segmentation → Change segmentation settings…** and review the tile size, padding and number of simultaneous tiles. Reducing tile size or concurrency can lower peak memory use. For a failed measurement task, **Change cell measurements…** lets you revise optional measurements and retain the accepted cells. [Memory estimates](stages/setup/memory-and-threads.md)

### Output storage is full or writing is interrupted

Pause active processing and free space on the drive named in the error. Keep the analysis folder and partially written output together. Image processing can resume from shards recorded as complete. A partial cell table must be rebuilt before the dataset can be selected. Reopen Run to see the recovery action for the saved attempt. [How outputs are completed and selected](stages/run/saving-and-recovery.md#v-restart-behaviour)

## Cluster submission outcome is uncertain

Reconnect and choose **Check submission** on Run. We use the saved receipt to find the original Slurm job. If it is found, Run reconnects to that job. If the scheduler has yet to return a matching record, choose **Keep waiting**. The cluster guide explains when to use [Allow another submission](stages/setup/computing.md#request-resources-and-submit).

If the job started but cannot read a file, check its path from inside the compute allocation. For local review, also check that the mounted folder maps to the same source, processed and paired images used by the job. Saved local annotation drafts remain in the workspace until their return completes.

## Reporting a problem

Include the affected stage, the complete error text and the last action that succeeded. Give the operating system, processing mode, application and native software versions, and relevant settings. A small shareable image or example can help reproduce a problem that depends on the source. Remove credentials and private patient information.

[Reopen and continue an analysis](stages/run/index.md#user-guide-reviewing-and-resuming)
