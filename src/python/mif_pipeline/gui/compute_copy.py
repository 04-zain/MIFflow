"""Labels and guidance for computing choices; serialized values remain unchanged."""

FIELDS = {
    "host": (
        "Cluster connection",
        "Enter user@host or your saved SSH connection name. Set up SSH key or agent access first.",
    ),
    "batch_executable": (
        "Pipeline program on the cluster",
        "Enter the full path to the installed mif-batch program. Activate the cluster Python environment and run command -v mif-batch to find this path.",
    ),
    "worker_executable": (
        "Processing program on the cluster",
        "Activate the cluster Python environment and run command -v mif-kernel-worker. Paste its full path here; this program receives selected model inputs and returns their results.",
    ),
    "manifest": (
        "Run definition",
        "Created automatically from Samples when the cluster analysis starts. Previously saved explicit locations are retained.",
    ),
    "output_root": (
        "Cluster parent folder",
        "Choose the cluster folder in which the named analysis folder will be created for processing and results.",
    ),
    "batch_id": (
        "Analysis name",
        "Use the analysis name saved in the setup file so that the correct analysis is started or resumed.",
    ),
    "working_directory": (
        "Job messages",
        "Job messages are execution logs saved under the same analysis folder by default. Previously saved separate log locations are retained.",
    ),
    "partition": (
        "Compute queue",
        "Optional. Enter the queue name used for this allocation, such as the institution’s GPU queue.",
    ),
    "account": (
        "Project account",
        "Optional. Enter the project or allocation account charged for this work.",
    ),
    "cpus": ("Processor cores", "Number of processor cores requested for the job."),
    "memory_gib": ("Memory (GiB)", "Amount of memory reserved on the cluster for this job."),
    "hours": ("Time limit (hours)", "Maximum running time requested from the cluster."),
    "gpus": ("Graphics processors", "Number of GPUs requested. Use zero for CPU-only processing."),
    "job_id": (
        "Job number",
        "The cluster assigns this number when a job is submitted. Use it to check progress or stop that job.",
    ),
    "model": (
        "KRONOS folder on the cluster",
        "Enter the folder containing the same KRONOS package selected on this computer.",
    ),
}


# ==============================================================================
#                                  Compute copy
# ==============================================================================


def processor_report(value):
    """Present the actual processing host and its tested tensor backends."""
    lines = [f"Processing computer: {value['host']} ({value['platform']})."]
    if value.get("error"):
        return "\n".join([*lines, "The tensor libraries could not be loaded: " + value["error"]])
    edition = "NVIDIA CUDA " + value["cuda_build"] if value.get("cuda_build") else "CPU"
    lines.append(f"Installed tensor libraries: PyTorch {value['torch']} ({edition}).")
    names = {"cpu": "CPU", "cuda": "NVIDIA GPU", "mps": "Apple Metal"}
    for item in value["processors"]:
        if item["device"] == "mps" and value["platform"] != "Darwin":
            continue
        name = names[item["device"]]
        if item.get("name"):
            name += " — " + item["name"]
        detail = item["detail"]
        if "free_bytes" in item:
            detail += f" {item['free_bytes'] / 1024**3:.1f} GiB of GPU memory available."
        lines.append(f"{name}: {detail}")
    lines.append("Each analysis step checks that its model and required precision support the selected processor.")
    return "\n".join(lines)


def status_message(action, value):
    if not isinstance(value, dict):
        return "The requested check has completed."
    if action == "capacity":
        memory = value.get("Available RAM (GiB)")
        storage = value.get("Free storage (GiB)")
        device = {"cpu": "CPU", "cuda": "NVIDIA GPU", "mps": "Apple Metal GPU"}.get(
            value.get("Available processing device"), "available processor"
        )
        available = (
            "Memory information is unavailable."
            if memory is None
            else f"{memory:g} GiB of memory available."
        )
        text = f"{available} {storage:g} GiB of free storage. Selected processor: {device}."
        if value.get("Processors"):
            text += "\n" + processor_report(value["Processors"])
        return text
    if action in {"check_setup", "check_environment"}:
        names = {
            "program": "Pipeline program",
            "setup": "Run definition",
            "results": "Cluster parent folder",
            "job_folder": "Job messages",
            "scheduler": "Slurm scheduler",
        }
        return "\n".join(
            f"{names.get(name,name)}: {'available' if state=='ready' else 'not found or inaccessible'}."
            for name, state in value.get("checks", {}).items()
        )
    if action == "submit":
        message = (
            f"Job {value.get('job_id','')} submitted for {value.get('batch_id','the analysis')}."
        )
        if value.get("Local receipt warning"):
            message += " The local job receipt could not be saved; record the job number."
        return message
    if action == "cancel":
        return f"A stop request was sent for job {value.get('job_id','')}."
    if action == "probe":
        return (
            "The cluster connection is ready and its processing software matches this application."
        )
    if action == "status":
        lines = []
        for row in str(value.get("scheduler", "")).splitlines()[:8]:
            fields = row.strip().split("|")
            if len(fields) >= 2:
                message = f"Job {fields[0]}: {fields[1].replace('_',' ').lower()}."
                if len(fields) > 2 and fields[2]:
                    message += f" Elapsed time: {fields[2]}."
                lines.append(message)
        return "\n".join(lines) or "The cluster has no current status for this job."
    if action == "analysis_status":
        state = str(value.get("resume_disposition", value.get("outcome", ""))).replace("_", " ")
        nodes = value.get("workflow_nodes", value.get("nodes", []))
        completed = sum(row.get("status") == "completed" for row in nodes if isinstance(row, dict))
        pending = sum(
            row.get("status") == "needs_interaction" for row in nodes if isinstance(row, dict)
        )
        return f"Analysis {state}. {completed} steps completed; {pending} awaiting review."
    return "The requested check has completed."
