"""Researcher-facing layout for computing settings; the dialog retains all actions.

The widget consumes the current presentation model and emits researcher actions through its
controller. Expensive native or scientific operations use managed jobs, while the Qt thread
retains the selected form and its response identity."""

# ==============================================================================
#                                    Imports
# ==============================================================================

import os
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QVBoxLayout,
    QHBoxLayout,
    QTabWidget,
    QComboBox,
    QLineEdit,
    QSpinBox,
    QDialogButtonBox,
    QToolButton,
    QLabel,
    QSizePolicy,
)
from .form_sections import FormSection, WorkbenchMessage, scroll_content, label
from .researcher_widgets import ContentSizedButton, configure_disclosure
from ..compute_copy import FIELDS

# ==============================================================================
#                                 Compute layout
# ==============================================================================


class _PathDetailsLabel(QLabel):
    """Keep every wrapped path line visible as the settings panel changes width."""

    def __init__(self, text, parent=None):
        super().__init__(text, parent)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setWordWrap(True)
        self.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.setProperty("choiceHelp", True)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Minimum)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.setMinimumHeight(max(0, self.heightForWidth(self.width())))

    def setText(self, text):
        super().setText(text)
        if self.isVisible():
            self.setMinimumHeight(max(0, self.heightForWidth(self.width())))


def build_compute_layout(page):
    layout = QVBoxLayout(page)
    layout.setContentsMargins(18, 16, 18, 16)
    layout.setSpacing(12)
    layout.addWidget(label("Computing", page, role="dialogHeading"))
    layout.addWidget(
        label(
            "Choose the processor and check the resources available for this analysis."
            if page.selected_mode == "local"
            else "Choose where to process the images and set up access to that computer.",
            page,
            role="choiceHelp",
        )
    )
    tabs = QTabWidget(page)
    tabs.setObjectName("compute-tabs")
    tabs.setDocumentMode(True)
    tabs.tabBar().setExpanding(True)
    tabs.setUsesScrollButtons(False)
    layout.addWidget(tabs, 1)
    scroll, local, body = scroll_content(page)
    processor = FormSection("Processing", parent=local)
    page.device = QComboBox(page)
    page.device.setObjectName("compute-device")
    for title, value in (
        ("Automatic", "auto"),
        ("CPU", "cpu"),
        ("NVIDIA GPU", "cuda"),
        ("Apple Metal GPU", "mps"),
    ):
        page.device.addItem(title, value)
    page.device.setCurrentIndex(
        max(
            0,
            page.device.findData(
                page._settings.get("device", os.environ.get("MIF_COMPUTE_DEVICE", "auto"))
            ),
        )
    )
    processor.field(
        "device",
        "Preferred processor",
        page.device,
        "Choose the processor for supported analysis steps.",
    )
    from ..cluster_guidance import CLUSTER_DESKTOP

    body.addWidget(processor)
    storage = FormSection("Storage", parent=local)
    page.folder = QLineEdit(str(page.workspace), page)
    storage.field(
        "storage-folder",
        "Check available storage in",
        page.folder,
        "To check free space on another drive, enter a folder on that drive.",
    )
    page.check = ContentSizedButton("Check memory, storage and processors", page)
    page.check.setObjectName("compute-check-capacity")
    page.check.clicked.connect(page.check_capacity)
    storage.body.addWidget(page.check, alignment=Qt.AlignmentFlag.AlignLeft)
    if page.manifest_path is not None:
        details_toggle = QToolButton(storage)
        details_toggle.setText("Details")
        details_toggle.setObjectName("compute-local-details-toggle")
        details_toggle.setCheckable(True)
        details = _PathDetailsLabel(
            "Run definition: " + str(page.manifest_path) + "\n"
            "The application creates this file from your saved samples and settings when preparing the Plan.",
            storage,
        )
        details.setObjectName("compute-local-run-definition")
        details.hide()
        details_toggle.toggled.connect(details.setVisible)
        storage.body.addWidget(details_toggle, alignment=Qt.AlignmentFlag.AlignLeft)
        storage.body.addWidget(details)
    body.addWidget(storage)
    body.addStretch(1)
    tabs.addTab(scroll, "This computer")
    desktop_scroll, desktop, desktop_body = scroll_content(page)
    desktop_section = FormSection("Work in the cluster desktop", CLUSTER_DESKTOP, desktop)
    from .cluster_help import show_cluster_setup

    requirements = ContentSizedButton("Required software and paths", desktop)
    requirements.clicked.connect(lambda: show_cluster_setup(page, mode="desktop"))
    desktop_section.body.addWidget(requirements, alignment=Qt.AlignmentFlag.AlignLeft)
    desktop_body.addWidget(desktop_section)
    desktop_body.addStretch(1)
    tabs.addTab(desktop_scroll, "Cluster desktop")
    scroll, remote, body = scroll_content(page)
    connection = FormSection(
        "Connection", "Connect to the cluster where this analysis will run.", remote
    )
    from .form_navigation import FormNavigation

    connection_actions = FormNavigation(connection)
    connection_actions.setObjectName("compute-connection-actions")
    page.remote_fields = {}
    for name in ("host", "batch_executable"):
        editor = QLineEdit(page)
        page.remote_fields[name] = editor
        connection.field(name, *FIELDS[name][:1], editor, FIELDS[name][1])
    body.addWidget(connection)
    storage = FormSection(
        "Analysis storage", "Choose where this analysis will be saved on the cluster.", remote
    )
    storage.setObjectName("compute-analysis-storage")
    results = QLineEdit(page)
    page.remote_fields["output_root"] = results
    page.cluster_storage_mode = QComboBox(storage)
    page.cluster_storage_mode.addItem("Create a new cluster analysis", "create")
    page.cluster_storage_mode.addItem("Open an existing cluster analysis", "open")
    storage.field("cluster-storage-mode", "Cluster analysis", page.cluster_storage_mode, "")
    page._cluster_create_path = storage.field(
        "output_root",
        "Cluster parent folder",
        results,
        "Existing writable folder in which the application will create this analysis. It must be accessible from the compute nodes.",
    )
    page.existing_cluster_folder = QLineEdit(storage)
    page.existing_cluster_folder.setPlaceholderText("/cluster/path/analysis-folder")
    page._cluster_existing_path = storage.field(
        "existing-cluster-folder",
        "Existing cluster analysis folder",
        page.existing_cluster_folder,
        "Full cluster path to the saved analysis folder. Its samples and progress will be opened in this local analysis.",
    )
    page.cluster_browse = ContentSizedButton("Browse mounted folder…", storage)
    page.cluster_browse.setObjectName("compute-existing-cluster-browse")
    page.cluster_browse.clicked.connect(page.browse_existing_cluster)
    storage.body.addWidget(page.cluster_browse, alignment=Qt.AlignmentFlag.AlignLeft)
    page.check_environment = ContentSizedButton("Check connection", connection_actions)
    page.check_environment.setProperty("actionRole", "primary")
    page.check_environment.setObjectName("compute-check-environment")
    page.check_environment.clicked.connect(lambda: page.remote_action("check_environment"))
    body.addWidget(storage)
    from .mounted_images import build_mounted_images_section

    build_mounted_images_section(page, remote, body)
    environment = FormSection(
        "Software environment (optional)",
        "If your environment is not configured by default on the cluster, provide the activation commands or software modules used to run mif-batch.",
        remote,
    )
    page.environment_script = QLineEdit(page)
    environment.field(
        "environment-script",
        "Environment script (optional)",
        page.environment_script,
        "Enter the cluster path to a shell script containing your environment activation commands. It must be accessible from the compute nodes. The script is sourced at the start of each Slurm job and before cluster mif-batch commands.",
    )
    page.modules = QLineEdit(page)
    environment.field(
        "modules",
        "Additional software modules",
        page.modules,
        "If the pipeline requires cluster modules, enter their names separated by spaces (for example, Java/21 CUDA/12.4). They are loaded after the activation script.",
    )
    software = environment
    page.software_paths = software
    page.runtime_fields = {}
    for name, title, help_text in (
        (
            "qupath",
            "QuPath program",
            "If QuPath is not configured on the cluster, enter the full path to its installed QuPath 0.7.0 program.",
        ),
        (
            "java-home",
            "Java folder",
            "If Java is not configured on the cluster, enter its Java 21 installation folder, which contains the bin folder.",
        ),
        (
            "bioformats-root",
            "Bio-Formats folder",
            "If Bio-Formats is not configured on the cluster, enter the folder selected when installing standalone Bio-Formats 8.5.0.",
        ),
    ):
        editor = QLineEdit(page)
        page.runtime_fields[name] = editor
        software.field(name, title + " (optional)", editor, help_text)
    body.addWidget(software)
    setup = FormSection("Analysis folder", parent=storage)
    page._analysis_folder_summary = setup
    setup.setProperty("workbenchSection", False)
    setup.body.setContentsMargins(0, 8, 0, 0)
    page.analysis_folder_text = label("", setup, role="choiceHelp")
    page.analysis_folder_text.setObjectName("compute-analysis-folder")
    setup.body.addWidget(page.analysis_folder_text)
    path_details_toggle = QToolButton(setup)
    path_details_toggle.setText("Details")
    path_details_toggle.setObjectName("compute-cluster-path-details-toggle")
    path_details_toggle.setCheckable(True)
    page.path_details = _PathDetailsLabel("", setup)
    page.path_details.setObjectName("compute-cluster-path-details")
    page.path_details.hide()
    path_details_toggle.toggled.connect(page.path_details.setVisible)
    setup.body.addWidget(path_details_toggle, alignment=Qt.AlignmentFlag.AlignLeft)
    setup.body.addWidget(page.path_details)
    for name in ("batch_id", "manifest", "working_directory"):
        editor = QLineEdit(page)
        editor.setReadOnly(True)
        editor.hide()
        page.remote_fields[name] = editor
    page.remote_fields["output_root"].textChanged.connect(page._update_derived_locations)
    page.remote_fields["batch_id"].textChanged.connect(page._update_derived_locations)
    page.prepare_samples = ContentSizedButton("Prepare samples from this Plan…", page)
    page.prepare_samples.setObjectName("compute-prepare-cluster")
    page.prepare_samples.clicked.connect(page.open_cluster_setup)
    setup.body.addWidget(page.prepare_samples, alignment=Qt.AlignmentFlag.AlignLeft)
    page.prepare_samples.hide()
    page.operation = QComboBox(page)
    page.operation.addItem("Start this analysis", "run")
    page.operation.addItem("Resume this analysis", "resume")
    page.operation.hide()
    storage.body.addWidget(setup)
    page.cluster_storage_mode.currentIndexChanged.connect(page._storage_choice_changed)
    resources = FormSection(
        "Resources",
        "Choose a starting allocation below. Use the job log to adjust memory or time for later runs; the cluster must have a compatible GPU allocation for GPU processing.",
        remote,
    )
    page.remote_device = QComboBox(page)
    for title, value in (("Automatic", "auto"), ("CPU", "cpu"), ("NVIDIA GPU", "cuda")):
        page.remote_device.addItem(title, value)
    resources.field(
        "cluster-device",
        "Cluster processor",
        page.remote_device,
        "Use a processor included in this job allocation.",
    )
    for index, (name, default, maximum) in enumerate(
        (("cpus", 4, 256), ("memory_gib", 16, 4096), ("hours", 4, 168), ("gpus", 0, 16))
    ):
        editor = QSpinBox(page)
        editor.setRange(0 if name == "gpus" else 1, maximum)
        editor.setValue(default)
        page.remote_fields[name] = editor
        resources.field(name, FIELDS[name][0], editor, FIELDS[name][1], column=index % 2, span=1)
    for name in ("partition", "account"):
        editor = QLineEdit(page)
        page.remote_fields[name] = editor
        resources.field(name, FIELDS[name][0], editor, FIELDS[name][1])
    body.addWidget(resources)
    tracking = FormSection(
        "Start processing",
        "Submit a new run or resume the saved analysis using the selected settings.",
        remote,
    )
    tracking.setObjectName("compute-run-section")
    page.load = ContentSizedButton("Load connection…", connection_actions)
    page.save_profile = ContentSizedButton("Save connection…", connection_actions)
    for button in (page.load, page.save_profile):
        button.setProperty("actionRole", "secondary")
    for button in (page.load, page.save_profile, page.check_environment):
        connection_actions.addWidget(button)
    from .action_bar import WorkflowActionBar

    connection.body.addWidget(connection_actions)
    page.job_id = QLineEdit(page)
    page.check_remote = ContentSizedButton("Check software and file paths", page)
    page.check_remote.setObjectName("compute-check-remote")
    page.check_remote.clicked.connect(lambda: page.remote_action("check_setup"))

    page.export_script = ContentSizedButton("Save Slurm script…", page)
    page.export_script.setObjectName("compute-export-script")
    page.export_script.clicked.connect(page.export_slurm_script)

    page.submit = ContentSizedButton("Run on cluster", page)
    page.submit.setObjectName("compute-remote-submit")
    page.submit.setProperty("actionRole", "primary")
    page.status = ContentSizedButton("Refresh job status", page)
    page.cancel = ContentSizedButton("Stop job", page)
    run_actions = WorkflowActionBar(tracking)
    run_actions.setObjectName("compute-run-actions")
    run_actions.add_primary(page.submit)
    run_actions.add_utility(page.check_remote)
    run_actions.add_utility(page.export_script)
    tracking.body.addWidget(run_actions)
    page.analysis_status = ContentSizedButton("View analysis progress", page)
    reviews = FormSection(
        "Complete requested reviews",
        "Open current forms and prepare annotation views when the analysis needs your input.",
        remote,
    )
    reviews.setObjectName("compute-review-section")
    page.cluster_reviews = ContentSizedButton("Open pending cluster reviews", page)
    page.cluster_reviews.setObjectName("compute-cluster-reviews")
    page.cluster_reviews.setProperty("actionRole", "secondary")
    page.cluster_reviews.clicked.connect(page.open_cluster_reviews)
    reviews.body.addWidget(page.cluster_reviews, alignment=Qt.AlignmentFlag.AlignLeft)
    progress = FormSection(
        "Job progress", "Check the submitted job or stop work that is no longer needed.", remote
    )
    progress.setObjectName("compute-job-section")
    progress.field("job_id", FIELDS["job_id"][0], page.job_id, FIELDS["job_id"][1])
    progress_actions = WorkflowActionBar(progress)
    progress_actions.setObjectName("compute-progress-actions")
    progress_actions.add_primary(page.status)
    progress_actions.add_primary(page.analysis_status)
    progress_actions.add_utility(page.cancel)
    progress.body.addWidget(progress_actions)
    for section in (tracking, reviews, progress):
        body.addWidget(section)
        section.setVisible(not page.embedded_workflow)
    body.addStretch(1)
    tabs.addTab(scroll, "Cluster streaming")
    page.load.clicked.connect(page.load_profile)
    page.save_profile.clicked.connect(page.export_profile)
    page.submit.clicked.connect(lambda: page.remote_action("submit"))
    page.status.clicked.connect(lambda: page.remote_action("status"))
    page.cancel.clicked.connect(lambda: page.remote_action("cancel"))
    page.analysis_status.clicked.connect(lambda: page.remote_action("analysis_status"))
    page.output = WorkbenchMessage(page)
    page.output.setObjectName("compute-status")
    layout.addWidget(page.output)
    actions = WorkflowActionBar(page)
    actions.setObjectName("compute-save-actions")
    save = ContentSizedButton(
        "Save and return" if page.embedded_workflow else "Save settings", page
    )
    save.setObjectName("compute-save")
    save.setProperty("actionRole", "primary")
    save.clicked.connect(page.save)
    close = ContentSizedButton("Close", page)
    close.clicked.connect(page.reject)
    actions.add_primary(save)
    actions.add_utility(close)
    layout.addWidget(actions)
    return tabs
