"""Local capacity, device choice and explicit remote job control.

The widget consumes the current presentation model and emits researcher actions through its
controller. Expensive native or scientific operations use managed jobs, while the Qt thread
retains the selected form and its response identity."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations
from pathlib import Path
import json, os, shutil
from PySide6.QtCore import Qt, QStandardPaths, Signal
from PySide6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QFormLayout,
    QTabWidget,
    QWidget,
    QLabel,
    QComboBox,
    QLineEdit,
    QSpinBox,
    QPushButton,
    QPlainTextEdit,
    QFileDialog,
    QMessageBox,
    QDialogButtonBox,
    QScrollArea,
)
from .jobs import JobAccess
from .researcher_widgets import ContentSizedButton
from mif_pipeline.orchestration.remote_compute import RemoteProfile, RemoteComputeClient
from mif_pipeline._capacity import available_memory_bytes

# ==============================================================================
#                               Compute workbench
# ==============================================================================


def compute_settings_path():
    return (
        Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppConfigLocation))
        / "compute-settings.json"
    )


def restore_compute_preference():
    from mif_pipeline._compute_preferences import saved_compute_preferences

    path = compute_settings_path()
    # Desktop processing is local or cluster-resident; old selective transfer is retired.
    os.environ.pop("MIF_SELECTIVE_COMPUTE_PROFILE", None)
    if not path.is_file() or path.stat().st_size > 65536:
        return
    try:
        settings = json.loads(path.read_text())
        device, precision = saved_compute_preferences(settings)
        os.environ.setdefault("MIF_COMPUTE_DEVICE", device)
        if "precision" in settings:
            os.environ.setdefault("MIF_COMPUTE_PRECISION", precision)
        os.environ["MIF_SPECTRAL_PRECISION"] = "device_selected"
    except (OSError, ValueError, TypeError, AttributeError):
        return


class ComputeWorkbench(QDialog):
    settings_saved = Signal(object)
    cluster_analysis_opened = Signal(object, object)

    def __init__(
        self,
        jobs,
        parent=None,
        *,
        settings_path=None,
        workspace=None,
        manifest_path=None,
        file_operations=None,
        sample_draft=None,
        selected_mode=None,
        embedded_workflow=False,
        analysis_setup=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Computing")
        self.resize(860, 740)
        self.selected_mode = selected_mode
        self.embedded_workflow = embedded_workflow
        if selected_mode not in {None, "local", "desktop", "streaming"}:
            raise ValueError("Unknown computing mode")
        self.file_operations = file_operations
        self.sample_draft = sample_draft
        self.analysis_setup = analysis_setup
        self.jobs = jobs
        self.settings_path = Path(settings_path or compute_settings_path())
        self.workspace = Path(workspace or Path.home())
        self._pending = {}
        self._submitted_profiles = {}
        self._profile = None
        self._settings = {}
        self.manifest_path = Path(manifest_path) if manifest_path is not None else None
        self._receipt_path = self.settings_path.with_suffix(".last-job.json")
        notice = ""
        if self.settings_path.is_file():
            try:
                if self.settings_path.stat().st_size > 65536:
                    raise ValueError("Settings file exceeds the supported size.")
                candidate = json.loads(self.settings_path.read_text())
                from mif_pipeline._compute_preferences import saved_compute_preferences

                saved_compute_preferences(candidate)
                self._settings = candidate
            except (OSError, ValueError, TypeError):
                notice = "The saved computing settings could not be read. Review the displayed settings and save them again."
                self._settings = {}
        from .compute_layout import build_compute_layout

        self.tabs = build_compute_layout(self)
        mode = selected_mode or self._settings.get("mode", "local")
        self.tabs.setCurrentIndex({"local": 0, "desktop": 1, "streaming": 2}.get(mode, 0))
        if embedded_workflow:
            self.tabs.tabBar().hide()
        jobs.job_succeeded.connect(self._success)
        jobs.job_failed.connect(self._failure)
        try:
            self._fill_profile(self._settings.get("remote"))
        except (ValueError, TypeError):
            notice = "The saved cluster settings are incomplete. Enter the current program and file paths before using the cluster."
        mounted = self._settings.get("mounted_images")
        if mounted is not None:
            try:
                from ..mounted_images import MountedImageAccess

                restored = MountedImageAccess.from_mapping(mounted)
                for key, value in restored.to_mapping().items():
                    self.mounted_fields[key].setText(
                        ""
                        if key == "host" and self._settings.get("mounted_host_override") is False
                        else value
                    )
            except (ValueError, TypeError):
                notice = (
                    "Review the saved mounted-image folder settings before preparing a connection."
                )
        if notice:
            self.output.setPlainText(notice)
        self._locations_derived = (
            self._settings.get(
                "paths_policy", "explicit" if self._settings.get("remote") else "derived"
            )
            == "derived"
        )
        if sample_draft is not None and self._locations_derived:
            self.remote_fields["batch_id"].setText(sample_draft.batch_id)
            self.remote_fields["batch_id"].setReadOnly(True)
        self._storage_choice_changed()
        if (
            not embedded_workflow
            and self._receipt_path.is_file()
            and self._receipt_path.stat().st_size <= 65536
        ):
            try:
                retained = json.loads(self._receipt_path.read_text())
                profile = RemoteProfile.from_mapping(retained["profile"])
                job = RemoteComputeClient._job(retained["receipt"]["job_id"])
                self._fill_profile(profile.to_mapping())
                self.job_id.setText(job)
            except (OSError, ValueError, KeyError):
                pass

    def _fill_profile(self, value):
        if value is None:
            return
        value = RemoteProfile.from_mapping(value).to_mapping()
        self._locations_derived = False
        for key, editor in self.remote_fields.items():
            if key in value:
                if isinstance(editor, QSpinBox):
                    editor.setValue(int(value[key]))
                else:
                    editor.setText(str(value[key]))
        for key, editor in self.runtime_fields.items():
            editor.setText(str(value.get("runtime_options", {}).get(key, "")))
        self.remote_device.setCurrentIndex(
            max(0, self.remote_device.findData(value.get("device", "auto")))
        )
        self.environment_script.setText(value.get("environment_script", ""))
        self.modules.setText(" ".join(value.get("modules", ())))
        self._profile = value

    def _update_derived_locations(self):
        """Show the selected cluster folder and refresh its generated locations.

        The accepted analysis name and cluster parent determine new run-definition
        and job-message paths. Saved explicit locations remain unchanged and are
        displayed in optional Details, retaining older cluster associations.
        """
        derived = getattr(self, "_locations_derived", True)
        for key in ("manifest", "working_directory"):
            self.remote_fields[key].setReadOnly(True)
        from pathlib import PurePosixPath

        root = self.remote_fields["output_root"].text().strip()
        name = self.remote_fields["batch_id"].text().strip()
        valid = bool(
            root
            and name
            and PurePosixPath(root).is_absolute()
            and ".." not in PurePosixPath(root).parts
        )
        self.analysis_folder_text.setText(str(PurePosixPath(root) / name) if valid else "")
        self._analysis_folder_summary.setVisible(
            valid and self.cluster_storage_mode.currentData() == "create"
        )
        if derived and root and name:
            workspace = PurePosixPath(root) / name
            self.remote_fields["manifest"].setText(str(workspace / "analysis.manifest.json"))
            self.remote_fields["working_directory"].setText(str(workspace))
        self.path_details.setText(
            "Run definition: " + self.remote_fields["manifest"].text() + "\n"
            "Job messages: " + self.remote_fields["working_directory"].text()
        )
        for key in ("manifest", "working_directory"):
            self.remote_fields[key].setReadOnly(True)

    def _storage_choice_changed(self):
        existing = self.cluster_storage_mode.currentData() == "open"
        previous = getattr(self, "_opening_existing_cluster", False)
        if existing and not previous:
            self._create_locations = (
                self._locations_derived,
                {
                    key: self.remote_fields[key].text()
                    for key in ("batch_id", "output_root", "manifest", "working_directory")
                },
            )
        elif previous and not existing:
            self._locations_derived, locations = self._create_locations
            for key, value in locations.items():
                self.remote_fields[key].setText(value)
        self._opening_existing_cluster = existing
        self._cluster_create_path.setVisible(not existing)
        self._cluster_existing_path.setVisible(existing)
        self.cluster_browse.setVisible(existing)
        if not existing and self.sample_draft is not None:
            self.remote_fields["batch_id"].setText(self.sample_draft.batch_id)
        self._update_derived_locations()
        button = self.findChild(QPushButton, "compute-save")
        if button is not None:
            button.setText(
                "Open cluster analysis"
                if existing
                else ("Save and return" if self.embedded_workflow else "Save settings")
            )

    def browse_existing_cluster(self):
        try:
            mounted = self.mounted_image_profile()
            selected = QFileDialog.getExistingDirectory(
                self, "Choose saved cluster analysis", mounted.local_folder
            )
            if selected:
                self.existing_cluster_folder.setText(mounted.cluster_path(selected))
        except ValueError as error:
            self.output.setPlainText(str(error))

    def profile(self):
        if self.cluster_storage_mode.currentData() == "open":
            from pathlib import PurePosixPath

            folder = PurePosixPath(self.existing_cluster_folder.text().strip())
            if not folder.is_absolute() or ".." in folder.parts or folder == folder.parent:
                raise ValueError("Enter the full path to the existing cluster analysis folder.")
            self._locations_derived = False
            for key, value in {
                "batch_id": folder.name,
                "output_root": str(folder.parent),
                "manifest": str(folder / "analysis.manifest.json"),
                "working_directory": str(folder),
            }.items():
                self.remote_fields[key].setText(value)
        self._update_derived_locations()
        values = dict(self._profile or {})
        values.update(
            {
                key: editor.value() if isinstance(editor, QSpinBox) else editor.text().strip()
                for key, editor in self.remote_fields.items()
            }
        )
        retained = {
            key: value
            for key, value in values.get("runtime_options", {}).items()
            if key not in self.runtime_fields
        }
        values["runtime_options"] = {
            **retained,
            **{
                key: editor.text().strip()
                for key, editor in self.runtime_fields.items()
                if editor.text().strip()
            },
        }
        values["environment_script"] = self.environment_script.text().strip()
        values["modules"] = tuple(self.modules.text().split())
        values["device"] = self.remote_device.currentData()
        from ..cluster_paths import with_selected_kronos_model
        from mif_pipeline.orchestration.setup.kronos_setup import selected_package

        mounted = self.mounted_image_profile() if selected_package() is not None else None
        return with_selected_kronos_model(RemoteProfile.from_mapping(values), mounted)

    def load_profile(self):
        file, _ = QFileDialog.getOpenFileName(self, "Load cluster profile", "", "JSON (*.json)")
        if not file:
            return
        try:
            path = Path(file)
            if path.stat().st_size > 65536:
                raise ValueError("Cluster profile is too large")
            value = RemoteProfile.from_mapping(json.loads(path.read_text())).to_mapping()
            chosen_locations = {
                key: self.remote_fields[key].text().strip()
                for key in ("output_root", "manifest", "working_directory")
            }
            derived = self._locations_derived
            self._locations_derived = False
            self._fill_profile(value)
            if self.embedded_workflow and self.sample_draft is not None:
                # Connection reuse keeps this analysis. Existing analyses reopen
                # from Home with their own retained execution-location record.
                self._locations_derived = derived
                self.remote_fields["batch_id"].setText(self.sample_draft.batch_id)
                self.remote_fields["batch_id"].setReadOnly(True)
                for key, location in chosen_locations.items():
                    if location:
                        self.remote_fields[key].setText(location)
                self._update_derived_locations()
                self.output.setPlainText(
                    "Connection settings loaded. This analysis name and results folder are retained."
                )
            else:
                self.remote_fields["batch_id"].setReadOnly(False)
                self._update_derived_locations()
        except (OSError, ValueError) as e:
            self.output.setPlainText(str(e))

    def export_profile(self):
        try:
            value = self.profile().to_mapping()
        except ValueError as e:
            self.output.setPlainText(str(e))
            return
        file, _ = QFileDialog.getSaveFileName(self, "Save cluster profile", "", "JSON (*.json)")
        if file:
            Path(file).write_text(json.dumps(value, indent=2))

    def open_cluster_setup(self):
        if self.jobs.has_active_jobs:
            return
        try:
            from pathlib import PurePosixPath

            root = self.remote_fields["output_root"].text().strip()
            name = self.remote_fields["batch_id"].text().strip()
            if not self.remote_fields["manifest"].text().strip() and root and name:
                self.remote_fields["manifest"].setText(
                    str(PurePosixPath(root) / name / "analysis.manifest.json")
                )
            client = RemoteComputeClient(self.profile())
            try:
                mounted = self.mounted_image_profile()
            except ValueError:
                mounted = None
            from .cluster_setup import ClusterSetupDialog
            from ..cluster_sample_projection import cluster_samples

            samples = cluster_samples(self.sample_draft, mounted)
            dialog = ClusterSetupDialog(client, self.jobs, mounted, parent=self, samples=samples)
            dialog.setWindowModality(Qt.WindowModality.WindowModal)
            self._cluster_setup_dialog = dialog
            dialog.show()
        except (OSError, ValueError) as error:
            self.output.setPlainText(str(error))

    def open_cluster_reviews(self):
        if self.jobs.has_active_jobs:
            return
        try:
            client = RemoteComputeClient(self.profile())
            try:
                mounted = self.mounted_image_profile()
            except ValueError:
                mounted = None
            from .cluster_reviews import ClusterReviewsDialog

            dialog = ClusterReviewsDialog(
                client,
                self.jobs,
                self,
                file_operations=self.file_operations,
                mounted_images=mounted,
                review_folder=self.settings_path.parent / "cluster-annotation-drafts",
            )
            dialog.setWindowModality(Qt.WindowModality.WindowModal)
            self._cluster_review_dialog = dialog
            dialog.show()
            dialog.refresh_reviews()
        except (OSError, ValueError) as error:
            self.output.setPlainText(str(error))

    def remote_action(self, action):
        try:
            profile = self.profile()
            client = RemoteComputeClient(profile)
            if (
                action == "cancel"
                and QMessageBox.question(
                    self,
                    "Cancel cluster job",
                    "Stop this cluster job? Any work completed since its last save may need to be repeated.",
                )
                != QMessageBox.StandardButton.Yes
            ):
                return
            operation = self.operation.currentData()
            job = self.job_id.text().strip()
            function = (
                (lambda: client.check_environment())
                if action == "check_environment"
                else (
                    (lambda: client.check_setup())
                    if action == "check_setup"
                    else (
                        (lambda: client.submit(operation))
                        if action == "submit"
                        else (
                            (lambda: client.analysis_status())
                            if action == "analysis_status"
                            else (lambda: getattr(client, action)(job))
                        )
                    )
                )
            )
            identity = self.jobs.submit(
                "remote-" + action,
                function,
                access=(
                    JobAccess.READ
                    if action in {"check_environment", "check_setup", "status", "analysis_status"}
                    else JobAccess.MUTATION
                ),
            )
            self._pending[identity] = action
            if action == "submit":
                self._submitted_profiles[identity] = profile.to_mapping()
            self.output.setPlainText("Contacting the configured cluster…")
        except (RuntimeError, ValueError) as e:
            self.output.setPlainText(str(e))

    def mounted_image_profile(self):
        from ..mounted_images import MountedImageAccess

        values = {key: editor.text().strip() for key, editor in self.mounted_fields.items()}
        if not values["host"]:
            values["host"] = self.remote_fields["host"].text().strip()
        return MountedImageAccess(**values)

    def prepare_mounted_images(self):
        try:
            command = self.mounted_image_profile().command()
        except ValueError as error:
            self.mount_command.clear()
            self.mount_command.hide()
            self.output.setPlainText(str(error))
            return
        self.mount_command.setPlainText(command)
        self.mount_command.show()
        self.output.setPlainText(
            "Connection command prepared. Create the empty local folder, then run the command with your installed SSHFS client. Use local QuPath to open the mounted image."
        )

    def export_slurm_script(self):
        try:
            script = RemoteComputeClient(self.profile()).slurm_script(self.operation.currentData())
            filename, _ = QFileDialog.getSaveFileName(
                self, "Save cluster job script", "mif-job.sh", "Shell script (*.sh)"
            )
            if filename:
                Path(filename).write_text(script, encoding="utf-8")
                self.output.setPlainText(
                    "Job script saved. Review it before submitting it through your cluster tools."
                )
        except (OSError, ValueError) as error:
            self.output.setPlainText(str(error))

    def check_capacity(self):
        folder = Path(self.folder.text()).expanduser()
        preferred = self.device.currentData()

        def inspect():
            free = shutil.disk_usage(folder).free
            memory = available_memory_bytes()
            from mif_pipeline._devices import runtime_capabilities, select_torch_device

            decision = select_torch_device("gnn", preference=preferred)
            return {
                "Available RAM (GiB)": None if memory is None else round(memory / 1024**3, 2),
                "Free storage (GiB)": round(free / 1024**3, 2),
                "Available processing device": decision.device,
                "Device details": decision.reason,
                "Processors": runtime_capabilities(),
                "Folder": str(folder),
            }

        try:
            identity = self.jobs.submit("compute-capacity", inspect, access=JobAccess.READ)
            self._pending[identity] = "capacity"
            self.output.setPlainText("Checking this processing environment…")
        except (RuntimeError, ValueError) as e:
            self.output.setPlainText(str(e))

    def _success(self, identity, value):
        if identity not in self._pending:
            return
        action = self._pending.pop(identity)
        if action == "associate-cluster":
            setup, settings = value
            self.cluster_analysis_opened.emit(setup, settings)
            self._settings = settings
            self.accept()
            return
        profile = self._submitted_profiles.pop(identity, None)
        if (
            action == "submit"
            and profile is not None
            and isinstance(value, dict)
            and "job_id" in value
        ):
            try:
                self._receipt_path.parent.mkdir(parents=True, exist_ok=True)
                temp = self._receipt_path.with_suffix(".tmp")
                temp.write_text(json.dumps({"profile": profile, "receipt": value}, indent=2))
                os.replace(temp, self._receipt_path)
            except OSError as error:
                value = {**value, "Local receipt warning": str(error)}
        if isinstance(value, dict) and "job_id" in value:
            self.job_id.setText(value["job_id"])
        from ..compute_copy import status_message

        self.output.setPlainText(status_message(action, value))

    def _failure(self, identity, error):
        if identity in self._pending:
            self._pending.pop(identity)
            self._submitted_profiles.pop(identity, None)
            self.output.setPlainText(getattr(error, "message", str(error)))

    def save(self):
        if self.jobs.has_active_jobs:
            self.output.setPlainText("Wait for active processing before changing its preferences.")
            return
        from mif_pipeline._compute_preferences import validate_compute_device
        from ..execution_location import save_location

        try:
            device = validate_compute_device(self.device.currentData())
            mode = self.selected_mode or ("local", "desktop", "streaming")[self.tabs.currentIndex()]
            value = {"mode": mode, "device": device}
            # Preserve the prior choice for pending requests that predate the
            # stage-specific control; new computing settings do not author it.
            if "precision" in self._settings:
                value["precision"] = self._settings["precision"]
            if mode == "streaming":
                value["remote"] = self.profile().to_mapping()
                value["paths_policy"] = "derived" if self._locations_derived else "explicit"
                if any(editor.text().strip() for editor in self.mounted_fields.values()):
                    value["mounted_images"] = self.mounted_image_profile().to_mapping()
                    value["mounted_host_override"] = bool(
                        self.mounted_fields["host"].text().strip()
                    )
            if mode == "streaming" and self.embedded_workflow:
                from ..execution_location import streaming_image_access

                streaming_image_access(value)
            if mode == "streaming" and self.cluster_storage_mode.currentData() == "open":
                if self.analysis_setup is None:
                    raise ValueError(
                        "Create a local analysis on Home before opening an existing cluster analysis here."
                    )
                from ..cluster_reopen import associate_cluster_analysis
                from pathlib import PurePosixPath

                mounted = self.mounted_image_profile()
                profile = RemoteProfile.from_mapping(value["remote"])
                folder = Path(
                    mounted.local_path(str(PurePosixPath(profile.output_root) / profile.batch_id))
                )
                choices = {
                    k: value[k]
                    for k in ("device", "precision", "mounted_host_override")
                    if k in value
                }
                setup = self.analysis_setup
                identity = self.jobs.submit(
                    "associate-cluster-analysis",
                    lambda: associate_cluster_analysis(
                        RemoteComputeClient(profile), folder, setup, mounted, choices
                    ),
                    access=JobAccess.MUTATION,
                )
                self._pending[identity] = "associate-cluster"
                self.output.setPlainText(
                    "Opening the saved samples and checking progress on the cluster…"
                )
                return
            saved = save_location(self.settings_path, value)
        except (OSError, ValueError) as error:
            self.output.setPlainText(str(error))
            return
        os.environ.pop("MIF_SELECTIVE_COMPUTE_PROFILE", None)
        os.environ["MIF_COMPUTE_DEVICE"] = device
        if "precision" in saved:
            os.environ["MIF_COMPUTE_PRECISION"] = saved["precision"]
        else:
            os.environ.pop("MIF_COMPUTE_PRECISION", None)
        os.environ["MIF_SPECTRAL_PRECISION"] = "device_selected"
        self._settings = saved
        self.output.setPlainText("Computing settings saved for this analysis.")
        self.settings_saved.emit(saved)
        if self.embedded_workflow:
            self.accept()

    def reject(self):
        if self._pending:
            self.output.setPlainText("Wait for the current connection or capacity check to finish.")
            return
        super().reject()
