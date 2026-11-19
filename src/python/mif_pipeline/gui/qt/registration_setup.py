"""

Presents separate Warpy-extension and Elastix/Transformix setup controls.

We inspect each component through its selected service, show the returned
state and expose only the installation or selection actions appropriate
to that state. Warpy installation handles its extension JAR; the program
setup handles the complete Elastix/Transformix installation.

Checks and file-changing operations run through the shared background-job
bridge. Their results update the corresponding labels and details on Qt's
thread. Software changes are disabled while an analysis or another setup
operation is active.
"""

# ==============================================================================
#                                    Imports
# ==============================================================================

from pathlib import Path
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget,
    QFrame,
    QVBoxLayout,
    QLabel,
    QPushButton,
    QFileDialog,
    QMessageBox,
)
from mif_pipeline.qupath_runtime.payload_lock import WARPY_SOURCE_ARCHIVE
from .form_sections import label
from .form_navigation import FormNavigation
from .researcher_widgets import ContentSizedButton
from .jobs import JobAccess
from .software_details import software_details_button

# ==============================================================================
#                               Registration setup
# ==============================================================================


class RegistrationSetupPanel(QWidget):
    """

    Connects the alignment-software status, download confirmation and installation actions.

    Attributes:
        busy_changed: Signals whether this panel has a pending software check,
            selection or installation operation.
    """

    busy_changed = Signal(bool)

    def __init__(
        self,
        jobs,
        *,
        warpy_factory,
        tools_service,
        on_changed=None,
        confirm_download=None,
        choose_directory=None,
        parent=None,
    ):
        """

        Builds the independent component sections and connects their background results.

        Each component receives a status, Install and Check again action, with
        additional existing-folder selection for Elastix/Transformix. Details remain
        separate from the status sentence and can be expanded for an incomplete
        or failed check. The initial inspection runs when the panel first appears.

        Args:
            jobs: Shared ManagedJobBridge delivering completion/failure signals.
            warpy_factory: Factory reflecting the currently selected QuPath program.
            tools_service: Elastix/Transformix inspection, selection and installation service.
            on_changed: Optional callback refreshing cached extension/setup information.
            confirm_download: Optional function confirming the displayed download text.
            choose_directory: Optional existing-program directory chooser.
            parent: Optional parent QWidget.
        """

        super().__init__(parent)
        self.setObjectName("registration-setup-panel")
        self._jobs = jobs
        self._factory = warpy_factory
        self._tools = tools_service
        self._changed = on_changed
        self._confirm = confirm_download or self._confirm_download
        self._choose = choose_directory or self._choose_folder
        self._pending = {}
        self._statuses = {}
        self._allowed = True
        self._analysis_open = False
        self._started = False
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(6)
        outer.setAlignment(Qt.AlignmentFlag.AlignTop)
        outer.addWidget(label("Paired-image alignment setup", self, role="panelHeading"))
        outer.addWidget(label(
            "Install the software needed to align paired images and display the aligned images.", self
        ))
        controls = QFrame(self)
        controls.setObjectName("registration-setup-controls")
        outer.addWidget(controls)
        layout = QVBoxLayout(controls)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(10)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._labels = {}
        self._install = {}
        self._check = {}
        self._details = {}
        self._details_buttons = {}
        for key, title in (("warpy", "Warpy extension"), ("tools", "Elastix and Transformix")):
            if key == "tools":
                layout.addSpacing(10)
            layout.addWidget(label(title, controls, role="panelHeading"))
            status = label("Not checked.", controls, name="registration-status-" + key)
            status.setProperty("setupReadiness", True)
            self._labels[key] = status
            layout.addWidget(status)
            row = FormNavigation(controls)
            install = ContentSizedButton("Install…", row)
            install.setObjectName("registration-install-" + key)
            install.setAccessibleName("Install " + title)
            install.setProperty("actionRole", "secondary")
            install.clicked.connect(lambda _checked=False, k=key: self._install_component(k))
            row.addWidget(install)
            self._install[key] = install
            if key == "tools":
                self._select = ContentSizedButton("Use existing…", row)
                self._select.setObjectName("registration-select-tools")
                self._select.clicked.connect(self._select_tools)
                row.addWidget(self._select)
            check = ContentSizedButton("Check again", row)
            check.setObjectName("registration-check-" + key)
            check.setProperty("actionRole", "primary")
            check.clicked.connect(lambda _checked=False, k=key: self.refresh(k))
            row.addWidget(check)
            self._check[key] = check
            layout.addWidget(row)
            button = software_details_button(controls)
            button.setObjectName("registration-details-button-" + key)
            button.setAccessibleName(title + " setup details")
            button.hide()
            button.clicked.connect(lambda _checked=False, k=key: self._open_details(k))
            row.addWidget(button)
            self._details[key] = ""
            self._details_buttons[key] = button
        jobs.job_succeeded.connect(self._succeeded)
        jobs.job_failed.connect(self._failed)
        self._buttons()

    def showEvent(self, event):
        """

        Starts the initial component checks when the software panel first becomes visible.

        Args:
            event: Qt show event passed to the normal QWidget handler.
        """

        super().showEvent(event)
        if not self._started:
            self._started = True
            self.refresh()

    def set_analysis_open(self, value):
        """

        Updates installation permissions when an analysis is opened or closed.

        Args:
            value: Whether an analysis is currently active.
        """

        self._analysis_open = bool(value)
        self._buttons()

    def set_actions_allowed(self, value):
        """

        Updates whether the computer and QuPath checks permit alignment-software setup.

        Args:
            value: Current setup-permission decision.
        """

        self._allowed = bool(value)
        self._buttons()

    def _buttons(self):
        """

        Enables actions from the current component states and pending-operation status.

        Check and existing-folder selection require setup permission, no open
        analysis and no pending panel operation. Install additionally requires a
        missing component, and Warpy requires a known extension destination.
        """

        permitted = self._allowed and not self._analysis_open and not self._pending
        for key in ("warpy", "tools"):
            status = self._statuses.get(key)
            ready = status is not None and status.state == "ready"
            self._check[key].setVisible(not ready)
            self._install[key].setVisible(not ready)
            if key == "tools":
                self._select.setText("Change location…" if ready else "Use existing…")
            self._check[key].setEnabled(permitted)
            self._install[key].setEnabled(
                permitted
                and status is not None
                and status.state == "missing"
                and (key == "tools" or status.location is not None)
            )
        self._select.setEnabled(permitted)

    def _service(self, key):
        """

        Retrieves the current Warpy or automatic-alignment program setup operation.

        Args:
            key: Warpy or tools component selected by the panel's controls.

        Returns:
            Current component service.

        Raises:
            ValueError: QuPath has not yet been selected for the requested setup.
        """

        service = self._factory() if key == "warpy" else self._tools
        if service is None:
            raise ValueError("Select QuPath in Software setup, then select Check again.")
        return service

    def refresh(self, key=None):
        """

        Checks one or both selected alignment components in the background.

        Each requested component returns its own status. A file or selection error
        becomes a not_checked status for that component, allowing the other check
        to be displayed rather than discarding both results.

        Args:
            key: Optional warpy or tools component. None checks both.
        """

        if self._pending:
            return
        keys = (key,) if key else ("warpy", "tools")

        def inspect():
            """

            Collects each requested component's current installation or loading result.

            Returns:
                Dictionary of component keys and status records, including a diagnostic
                status for an individual check that raised a path or selection error.
            """

            result = {}
            for k in keys:
                try:
                    result[k] = self._service(k).inspect()
                except (OSError, ValueError) as error:
                    from mif_pipeline.orchestration.setup.warpy_setup import WarpySetupStatus

                    result[k] = WarpySetupStatus("not_checked", None, str(error))
            return result

        self._submit("inspect", keys, inspect, False)

    def _submit(self, operation, keys, function, write):
        """

        Starts one panel operation and records which component labels should receive its result.

        We assign read access to inspection and mutation access to installation or
        selection. After the job starts, its ID and component keys are retained,
        the busy signal is emitted and the corresponding status labels describe
        the current action. A start failure is displayed without recording a pending job.

        Args:
            operation: Inspect, install or select action.
            keys: Component keys affected by the action.
            function: Callable performing the check or file operation.
            write: Whether the action changes installed files or saved selection.
        """

        if self._pending:
            return
        try:
            identity = self._jobs.submit(
                "registration-setup-" + operation,
                function,
                access=JobAccess.MUTATION if write else JobAccess.READ,
            )
            self._pending[identity] = (operation, keys)
            self.busy_changed.emit(True)
            for key in keys:
                self._labels[key].setText(
                    "Checking installed software…"
                    if operation == "inspect"
                    else "Installing and checking software…"
                )
        except RuntimeError as error:
            for key in keys:
                self._labels[key].setText(str(error))
        self._buttons()

    def _install_component(self, key):
        """

        Confirms the selected missing component's download before submitting installation.

        We obtain the selected release address, size and destination and include
        them in the confirmation text. Only the chosen component service is called,
        and it independently rechecks the existing files before writing its result.

        Args:
            key: Warpy or tools component selected by its Install button.
        """

        if self._pending or not self._allowed or self._analysis_open:
            return
        current = self._statuses.get(key)
        if current is None or current.state != "missing":
            return
        if key == "warpy":
            url, size, _ = WARPY_SOURCE_ARCHIVE
            title = "Warpy 0.5.0"
            target = current.location
        else:
            url = self._tools.pin["url"]
            size = self._tools.pin["size_bytes"]
            title = "Elastix and Transformix 5.3.1"
            target = self._tools.target
        text = f"Install {title}?\n\nDownload: {size/1024**2:.1f} MiB\nSource: {url}\nFolder: {target}\n\nThe downloaded files will be verified before installation."
        if not self._confirm(text):
            return

        def install():
            """

            Runs the confirmed installation and returns its status under the selected component key.

            Returns:
                One-component result mapping for the completion handler.
            """

            return {key: self._service(key).install(consent=True)}

        self._submit("install", (key,), install, True)

    def _select_tools(self):
        """

        Checks and saves an existing Elastix/Transformix directory selected by the folder chooser.

        Cancelling leaves the current selection unchanged. A chosen directory is
        passed to the program setup service as a background selection operation.
        """

        if self._pending or not self._allowed or self._analysis_open:
            return
        folder = self._choose()
        if folder is None:
            return
        self._submit(
            "select", ("tools",), lambda: {"tools": self._tools.select(Path(folder))}, True
        )

    def _succeeded(self, identity, value):
        """

        Displays the component results returned by a pending setup operation.

        We place the first diagnostic line beside its controls and keep subsequent
        lines and the relevant path in expandable Details. The pending job is then
        removed and action permissions are recalculated. Completed installation or
        selection, and a fresh Warpy check, notify the cache-refresh callback.

        Args:
            identity: Completed background-job ID.
            value: Dictionary mapping the affected component keys to their statuses.
        """

        job = self._pending.pop(identity, None)
        if job is None:
            return
        operation, keys = job
        for key in keys:
            status = value[key]
            self._statuses[key] = status
            summary, _, detail = status.detail.partition("\n")
            self._labels[key].setText(("Ready: " if status.state == "ready" else "") + summary)
            if status.location and str(status.location) not in status.detail:
                detail += "\nLocation: " + str(status.location)
            self._details[key] = detail.strip()
            button = self._details_buttons[key]
            button.setVisible(status.state not in ("ready", "missing") and bool(detail.strip()))
        self.busy_changed.emit(False)
        self._buttons()
        if self._changed and (operation in ("install", "select") or "warpy" in keys):
            self._changed()

    def _failed(self, identity, error):
        """

        Shows a background setup failure beside every component affected by that operation.

        We retain the complete diagnostic in visible Details, clear the pending
        operation and restore whichever actions the current setup state permits.

        Args:
            identity: Failed background-job ID.
            error: Background failure carrying its diagnostic or error message.
        """

        job = self._pending.pop(identity, None)
        if job is None:
            return
        operation, keys = job
        for key in keys:
            self._statuses.pop(key, None)
            self._labels[key].setText(
                "Setup could not finish. Open Details for the cause and next step."
            )
            self._details[key] = getattr(error, "diagnostic", None) or getattr(error, "message", str(error))
            self._details_buttons[key].setVisible(True)
        self.busy_changed.emit(False)
        self._buttons()

    def _open_details(self, key):
        from .software_details import open_software_details
        title = "Warpy extension" if key == "warpy" else "Elastix and Transformix"
        summary = self._labels[key].text()
        self._details_dialog = open_software_details(self, title, summary, self._details[key])

    def _choose_folder(self):
        """

        Opens the chooser for an extracted Elastix release directory or its program folder.

        Returns:
            Selected Path, or None when the chooser is cancelled.
        """

        selected = QFileDialog.getExistingDirectory(
            self, "Choose the extracted Elastix 5.3.1 folder or its bin folder"
        )
        return Path(selected) if selected else None

    def _confirm_download(self, text):
        """

        Asks for explicit confirmation before the selected alignment-software download.

        Args:
            text: Component version, source, byte count and destination explanation.

        Returns:
            True only when Yes is selected; No is the default action.
        """

        return (
            QMessageBox.question(
                self,
                "Install alignment software",
                text,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            == QMessageBox.StandardButton.Yes
        )
