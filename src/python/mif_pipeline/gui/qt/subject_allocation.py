"""Display and edit the exact subject roles submitted by the training form."""

from __future__ import annotations

import re
from html import escape
from PySide6.QtCore import Qt, Signal, QSignalBlocker, QMimeData, QSize, QTimer
from PySide6.QtGui import QDrag
from PySide6.QtWidgets import (
    QApplication, QAbstractItemView, QComboBox, QFrame, QGridLayout, QHeaderView, QHBoxLayout, QLabel,
    QLineEdit, QMenu, QScrollArea, QSizePolicy,
    QTabBar, QToolButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget, QWidgetItem,
)

from mif_pipeline.gui.training_procedure import LABELS, selected_procedure
from mif_pipeline.orchestration.batch_training.split_policy import assign_subject_roles
from mif_pipeline.pipeline_core.model_selection import subject_folds
from .researcher_widgets import ContentSizedButton


def _label(text, parent, *, error=False):
    label = QLabel(text, parent)
    label.setWordWrap(True)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setProperty("validationMessage" if error else "choiceHelp", True)
    return label


def _table(headers, parent, name):
    table = QTableWidget(0, len(headers), parent)
    table.setObjectName(name)
    table.setProperty("scientificTable", True)
    table.setAccessibleName(name.replace("-", " "))
    table.setHorizontalHeaderLabels(headers)
    table.verticalHeader().hide()
    table.setShowGrid(False)
    table.setAlternatingRowColors(True)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
    return table


def _fit_height(table):
    table.ensurePolished()
    table.resizeRowsToContents()
    for row in range(table.rowCount()):
        table.setRowHeight(row, max(48, table.rowHeight(row)))
    height = table.horizontalHeader().sizeHint().height() + sum(
        table.rowHeight(row) for row in range(table.rowCount())) + 8
    table.setFixedHeight(min(420, max(90, height)))


def _reference_names(values, index, fixed_names=()):
    if fixed_names:
        return fixed_names
    prefix = f"primary_{index}"
    count = int(values.get(prefix + "_class_count") or 0)
    names = [str(values.get(f"{prefix}_class_{row}_name") or f"Class {row + 1}") for row in range(count)]
    if values.get(prefix + "_ignore_policy") == "annotated":
        names.append("Ignore")
    return tuple(names)


class SubjectCard(QFrame):
    """Select one subject's images or move all of them to another split."""

    def __init__(self, owner, subject, parent=None):
        super().__init__(parent)
        self.owner, self.subject = owner, subject
        self.setObjectName("subject-card")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName(f"Subject {subject}")
        self.setAccessibleDescription(
            "Select to inspect images. Press Space to change the group, or drag the whole subject."
            if owner._can_move else "Select to inspect this subject's images."
        )
        self.setStyleSheet(
            "QFrame#subject-card { border: 1px solid #d9e3e4; border-radius: 6px; background: #ffffff; }"
            "QFrame#subject-card:hover { border-color: #8eacaf; background: #f4faf9; }"
            "QFrame#subject-card[selected=true] { border-color: #007d86; background: #eaf5f4; }"
            "QFrame#subject-card:focus { border: 2px solid #007d86; }")
        if owner._integrated_procedure:
            self.setStyleSheet(
                "QFrame#subject-card { border: none; border-bottom: 1px solid #cfdddb; background: transparent; }"
                "QFrame#subject-card:hover { background: #eef5f3; }"
                "QFrame#subject-card[selected=true] { background: #dceee8; border-left: 3px solid #007d80; }"
                "QFrame#subject-card:focus { border-bottom: 2px solid #007d80; }")
        self.setCursor(Qt.CursorShape.OpenHandCursor if owner._can_move else Qt.CursorShape.PointingHandCursor)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(2)
        top = QHBoxLayout()
        name = _label(subject, self)
        name.setProperty("choiceHelp", False)
        name.setStyleSheet("font-weight: 600; color: #173d47;")
        name.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        top.addWidget(name, 1)
        entries = owner.subjects[subject]
        inventory = f"{len(entries)} {'image' if len(entries) == 1 else 'images'}"
        fold = owner._fold_assignments.get(subject)
        if owner._integrated_procedure and fold is not None and owner.fold_selector.currentData() is None:
            inventory = f"Fold {fold + 1} · {inventory}"
            self.setAccessibleDescription(f"Validates in fold {fold + 1}. " + self.accessibleDescription())
        count = _label(inventory, self)
        count.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        top.addWidget(count)
        self.more = None
        if owner._integrated_procedure and owner._can_move:
            self.more = QToolButton(self)
            self.more.setText("⋯")
            self.more.setFixedSize(28, 24)
            self.more.setAccessibleName(f"Move {subject} to another group")
            self.more.setToolTip("Move subject to another group")
            self.more.setCursor(Qt.CursorShape.PointingHandCursor)
            self.more.setStyleSheet("QToolButton { border:none; background:transparent; padding:0; font-size:18px; }"
                                    "QToolButton:hover, QToolButton:focus { background:#c8e1dc; }")
            policy = self.more.sizePolicy()
            policy.setRetainSizeWhenHidden(True)
            self.more.setSizePolicy(policy)
            self.more.clicked.connect(self._open_move_menu)
            top.addWidget(self.more)
        layout.addLayout(top)
        self._press = None
        self.select(subject == owner._selected)

    def _open_move_menu(self):
        self.owner.select_subject(self.subject)
        self.owner.group_menu.popup(self.more.mapToGlobal(self.more.rect().bottomLeft()))

    def contextMenuEvent(self, event):
        if self.more is not None:
            self.owner.select_subject(self.subject)
            self.owner.group_menu.popup(event.globalPos())
            event.accept()
        else:
            super().contextMenuEvent(event)

    def select(self, selected):
        if self.more is not None:
            self.more.setVisible(selected)
        self.setProperty("selected", selected)
        self.style().unpolish(self)
        self.style().polish(self)

    def focusInEvent(self, event):
        super().focusInEvent(event)
        self.owner.select_subject(self.subject)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._press = event.position().toPoint()
            self.setFocus()
            self.owner.select_subject(self.subject)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if (self.owner._can_move and self._press is not None and event.buttons() & Qt.MouseButton.LeftButton
                and (event.position().toPoint() - self._press).manhattanLength() >= QApplication.startDragDistance()):
            drag = QDrag(self)
            mime = QMimeData()
            mime.setData("application/x-mif-subject", self.subject.encode("utf-8"))
            drag.setMimeData(mime)
            drag.setPixmap(self.grab())
            drag.setHotSpot(self._press)
            self._press = None
            drag.exec(Qt.DropAction.MoveAction)
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._press = None
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.owner.select_subject(self.subject)
            event.accept()
        elif event.key() == Qt.Key.Key_Space and self.owner._can_move:
            self.owner.select_subject(self.subject)
            if self.owner._integrated_procedure:
                self._open_move_menu()
            else:
                self.owner.move_role.setFocus()
            event.accept()
        else:
            super().keyPressEvent(event)


class SubjectGroup(QFrame):
    """Move a dragged subject through the existing role editor from anywhere in a group."""

    def __init__(self, owner, role, parent=None):
        super().__init__(parent)
        self.owner, self.role = owner, role
        self.setObjectName("subject-group")
        self.setAcceptDrops(True)
        accent = {"training": "#007d86", "test": "#496ba0"}.get(role, "#91a3a7")
        self.setStyleSheet(
            "QFrame#subject-group { background: #f6f8f8; border: 1px solid #d7e1e2;"
            f" border-top: 3px solid {accent}; border-radius: 7px; }}"
            "QFrame#subject-group[dropTarget=true] { background: #e4f2ef; border-color: #007d86; }"
        )

    def _dragged_subject(self, event):
        """Accept cards from this allocation when their role would change."""
        source = event.source()
        if (self.owner._can_move and isinstance(source, SubjectCard)
                and source.owner is self.owner
                and self.owner.move_role.findData(self.role) >= 0
                and self.owner.role_by_subject().get(source.subject) != self.role):
            return source.subject
        return None

    def _highlight(self, active):
        self.setProperty("dropTarget", active)
        self.style().unpolish(self)
        self.style().polish(self)

    def dragEnterEvent(self, event):
        accepted = self._dragged_subject(event) is not None
        self._highlight(accepted)
        if accepted:
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        self.dragEnterEvent(event)

    def dragLeaveEvent(self, event):
        self._highlight(False)
        event.accept()

    def dropEvent(self, event):
        subject = self._dragged_subject(event)
        self._highlight(False)
        if subject is None:
            event.ignore()
            return
        self.owner.select_subject(subject)
        self.owner._set_roles({subject: self.role})
        self.owner._cards[subject].setFocus()
        event.acceptProposedAction()


class SubjectLane(QScrollArea):
    """Keep a group's cards within a bounded, vertically scrolling area."""

    def __init__(self, owner, role, parent=None):
        super().__init__(parent)
        self.owner, self.role = owner, role
        self.owner = owner
        self.setObjectName("subject-lane-" + str(role or "unassigned"))
        self.setAccessibleName("Subjects assigned to " + str(role or "unassigned"))
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setAcceptDrops(False)
        self.viewport().setAcceptDrops(False)
        self.setMinimumHeight(64)
        self.setMaximumHeight(260)
        body = QWidget(self)
        self.cards = QVBoxLayout(body)
        self.cards.setContentsMargins(0, 2, 6, 2)
        self.cards.setSpacing(0 if owner._integrated_procedure else 7)
        self.cards.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.setWidget(body)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.owner._integrated_procedure and event.size().width() != event.oldSize().width():
            QTimer.singleShot(0, self.owner, self.owner._size_lanes)


class SubjectAllocation(QWidget):
    """Subject cards share the existing form role editors and split allocator."""

    changed = Signal()
    image_selected = Signal(str)

    def __init__(self, form, index, parent=None):
        super().__init__(parent)
        self.setObjectName(f"subject-allocation-{index}")
        self.index = index
        self.prefix = f"training_{index}"
        self._integrated_procedure = any(form.context.get(route) for route in ("primary", "secondary", "supervised"))
        self._show_reference_counts = not form.context.get("supervised")
        context = form.context["resume_context"]
        candidate = sorted(context["candidate_entry_ids"])[index]
        secondary = context.get("secondary_output_classes_by_candidate_id", {}).get(candidate)
        self.reference_names = tuple(row["display_name"] for row in secondary["output_classes"]) if secondary else ()
        self.classifier_label = ", ".join(row["display_name"] for row in secondary["structures"]) if secondary else candidate
        self.permitted_parents = tuple(row["display_name"] for row in secondary["eligible_parents"]) if secondary else ()
        self.participants = context["candidate_participants"]
        grouped = {}
        for entry in sorted(context["candidate_entry_ids"][candidate]):
            grouped.setdefault(self.participants[entry]["subject_id"], []).append(entry)
        self.subjects = {subject: tuple(grouped[subject]) for subject in sorted(grouped)}
        self.roles = {}
        self._values = None
        self._procedure = None
        self._test_field = None
        self._material = None
        self._detail_material = None
        self._saved_regions = {}
        self._fold_assignments = {}
        self._fold_groups = []
        self._can_move = False
        self._selected = next(iter(self.subjects), None)
        self._cards = {}
        self._bindings = QWidget(self)
        self._bindings.hide()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.cohort = None
        if not self._integrated_procedure:
            self.cohort = QFrame(self)
            self.cohort.setObjectName(f"subject-cohort-{index}")
            self.cohort.setProperty("workbenchMessage", True)
            cohort_layout = QVBoxLayout(self.cohort)
            cohort_layout.setContentsMargins(12, 10, 12, 10)
            cohort_layout.setSpacing(8)
            self.procedure = _label("", self.cohort)
            self.procedure.setStyleSheet("font-weight: 600; color: #173d47;")
            cohort_title = QHBoxLayout()
            cohort_title.setSpacing(16)
            cohort_title.addWidget(self.procedure, 2)
            self.summary = _label("", self.cohort)
            self.summary.setObjectName(f"subject-allocation-summary-{index}")
            self.summary.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
            cohort_title.addWidget(self.summary, 1)
            cohort_layout.addLayout(cohort_title)
        self.automatic_host = QWidget(self)
        self.automatic_row = QHBoxLayout(self.automatic_host)
        self.automatic_row.setContentsMargins(0, 0, 0, 0)
        self.automatic_row.setSpacing(12)
        self.automatic = ContentSizedButton("Assign automatically", self)
        self.automatic.setObjectName(f"subject-assign-automatically-{index}")
        self.automatic.clicked.connect(self.assign_automatically)
        self.automatic_row.addWidget(self.automatic, alignment=Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        if self.cohort is not None:
            cohort_layout.addWidget(self.automatic_host)
            layout.addWidget(self.cohort)
        self._fold_host = None
        self.search = QLineEdit(self)
        self.search.setObjectName(f"subject-search-{index}")
        self.search.setAccessibleName("Find a subject or image")
        self.search.setPlaceholderText("Find a subject or image")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._filter_cards)
        self.search.setVisible(len(self.subjects) > 8)
        layout.addWidget(self.search)
        self.search_status = _label("", self)
        self.search_status.setObjectName(f"subject-search-status-{index}")
        self.search_status.hide()
        layout.addWidget(self.search_status)
        self.move_hint = _label("Drag subjects between groups, or select a subject and use Move to below.", self)
        layout.addWidget(self.move_hint)
        self.board = QWidget(self)
        self.board_layout = QGridLayout(self.board)
        self.board_layout.setContentsMargins(0, 0, 0, 0)
        self.board_layout.setSpacing(12)
        self.lanes = {}
        self.lane_hosts = {}
        self.lane_titles = {}
        self.lane_counts = {}
        self.lane_help = {}
        for role in ("training", "test", "not_selected", None):
            host = SubjectGroup(self, role, self.board)
            column = QVBoxLayout(host)
            column.setContentsMargins(12, 10, 12, 10)
            column.setSpacing(8)
            column.setAlignment(Qt.AlignmentFlag.AlignTop)
            title = QLabel(host)
            title.setWordWrap(True)
            title.setProperty("panelHeading", True)
            column.addWidget(title)
            count = _label("", host)
            column.addWidget(count)
            detail = _label("", host)
            column.addWidget(detail)
            if role == "training":
                self.fold_preview = QWidget(host)
                self.fold_preview.setObjectName(f"subject-validation-folds-{index}")
                preview = QVBoxLayout(self.fold_preview)
                preview.setContentsMargins(0, 0, 0, 0)
                preview.setSpacing(5)
                self.fold_selector = QComboBox(self.fold_preview)
                self.fold_selector.setObjectName(f"subject-validation-fold-{index}")
                self.fold_selector.setAccessibleName("Inspect planned validation fold")
                self.fold_selector.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
                self.fold_selector.setMinimumContentsLength(12)
                self.fold_selector.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
                self.fold_selector.setToolTip("Inspect which complete subjects validate or train in each planned fold. "
                    "Each subject validates once; all subjects in this group enter the final refit.")
                self.fold_selector.currentIndexChanged.connect(lambda: self.refresh())
                if self._integrated_procedure:
                    self.fold_tabs = QTabBar(self.fold_preview)
                    self.fold_tabs.setAccessibleName("Validation fold")
                    self.fold_tabs.setDocumentMode(True)
                    self.fold_tabs.setStyleSheet("QTabBar::tab { padding: 7px 7px; }")
                    self.fold_tabs.setExpanding(False)
                    self.fold_tabs.setUsesScrollButtons(True)
                    self.fold_tabs.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
                    self.fold_tabs.currentChanged.connect(self.fold_selector.setCurrentIndex)
                    preview.addWidget(self.fold_tabs)
                else:
                    preview.addWidget(self.fold_selector)
                self.fold_status = _label("", self.fold_preview)
                preview.addWidget(self.fold_status)
                column.addWidget(self.fold_preview)
            lane = SubjectLane(self, role, host)
            column.addWidget(lane)
            column.addStretch(1)
            self.lanes[role], self.lane_hosts[role] = lane, host
            self.lane_titles[role], self.lane_counts[role], self.lane_help[role] = title, count, detail
            self.board_layout.addWidget(host, 0, len(self.lane_hosts) - 1)
        layout.addWidget(self.board)
        self.detail = QFrame(self)
        self.detail.setObjectName(f"subject-detail-{index}")
        self.detail.setProperty("workbenchSection", not self._integrated_procedure)
        detail_layout = QVBoxLayout(self.detail)
        if self._integrated_procedure:
            detail_layout.setContentsMargins(24, 3, 12, 6)
        else:
            detail_layout.setContentsMargins(12, 10, 12, 10)
        detail_layout.setSpacing(8)
        self.selected = _label("", self.detail)
        self.selected.setObjectName(f"subject-selected-{index}")
        self.selected.setStyleSheet("font-weight: 600; color: #173d47;")
        detail_layout.addWidget(self.selected)
        self.selected_fold_role = _label("", self.detail)
        self.selected_fold_role.setObjectName(f"subject-selected-fold-{index}")
        self.selected_fold_role.hide()
        detail_layout.addWidget(self.selected_fold_role)
        self.move_controls = QWidget(self.detail)
        move = QHBoxLayout(self.move_controls)
        move.setContentsMargins(0, 0, 0, 0)
        move.addWidget(QLabel("Move to", self))
        self.move_role = QComboBox(self)
        self.move_role.setObjectName(f"subject-move-role-{index}")
        self.move_role.setAccessibleName("Move selected subject to")
        move.addWidget(self.move_role, 1)
        self.move_button = ContentSizedButton("Move subject", self)
        self.move_button.setObjectName(f"subject-move-{index}")
        self.move_button.clicked.connect(self._move_selected)
        self.move_role.currentIndexChanged.connect(lambda: self.select_subject(self._selected))
        move.addWidget(self.move_button)
        detail_layout.addWidget(self.move_controls)
        self.image_heading = QWidget(self.detail)
        heading = QGridLayout(self.image_heading)
        heading.setContentsMargins(0, 0, 0, 0)
        heading.setHorizontalSpacing(16)
        heading.addWidget(_label("Images", self.image_heading), 0, 0)
        self.annotation_heading = _label("Returned annotation regions", self.image_heading)
        heading.addWidget(self.annotation_heading, 0, 1)
        if self._integrated_procedure:
            self.group_menu = QMenu(self)
            self.group_menu.aboutToShow.connect(self._list_destinations)
            self.group_menu.triggered.connect(lambda action: self._set_roles({self._selected: action.data()}))
        self.annotation_heading.hide()
        heading.setColumnStretch(0, 1)
        heading.setColumnStretch(1, 2)
        detail_layout.addWidget(self.image_heading)
        self.image_rows = QVBoxLayout()
        self.image_rows.setSpacing(0)
        detail_layout.addLayout(self.image_rows)
        layout.addWidget(self.detail)
        self.message = _label("", self, error=True)
        self.message.setObjectName(f"subject-assignment-message-{index}")
        self.message.hide()
        layout.addWidget(self.message)

    def add_control(self, name, field, editor):
        """Bind the original editors; the board is their only visible presentation."""
        if name.endswith("_test_subjects"):
            self._test_field = field
            field.layout().setContentsMargins(0, 0, 0, 0)
            field.layout().setSpacing(0)
            self._test_host = QWidget(self)
            column = QHBoxLayout(self._test_host)
            column.setContentsMargins(0, 0, 0, 0)
            column.setSpacing(8)
            caption = QLabel("Subjects to reserve" if self._integrated_procedure else "Number of subjects for independent testing", self._test_host)
            caption.setProperty("stageFieldCaption", True)
            caption.setBuddy(editor)
            column.addWidget(caption)
            field.setFixedWidth(74 if self._integrated_procedure else 90)
            column.addWidget(field, alignment=Qt.AlignmentFlag.AlignVCenter)
            if self._integrated_procedure:
                self.automatic_row.insertWidget(0, self._test_host)
                self.automatic_row.addStretch(1)
            else:
                self.automatic_row.insertWidget(0, self._test_host, 1)
            return
        if name.endswith("_cv_folds"):
            self._fold_host = QWidget(self)
            row = QHBoxLayout(self._fold_host)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(8)
            caption = QLabel("Validation folds", self._fold_host)
            caption.setBuddy(self.fold_choice)
            caption.setProperty("stageFieldCaption", True)
            field.layout().setContentsMargins(0, 0, 0, 0)
            row.addWidget(caption)
            if self._integrated_procedure:
                self.fold_choice.setMinimumContentsLength(2)
                field.setFixedWidth(max(74, self.fold_choice.sizeHint().width()))
                row.addWidget(field)
                row.addStretch(1)
                self.lane_hosts["training"].layout().insertWidget(2, self._fold_host)
            else:
                field.setMinimumWidth(120)
                field.setMaximumWidth(190)
                row.addWidget(field)
                row.addStretch(1)
                self.cohort.layout().addWidget(self._fold_host)
            return
        match = re.fullmatch(self.prefix + r"_patient_(\d+)_role", name)
        if match is None:
            raise ValueError("Unexpected subject-allocation control")
        subject = tuple(self.subjects)[int(match.group(1))]
        self.roles[subject] = editor
        field.setParent(self._bindings)
        editor.currentIndexChanged.connect(self.changed)

    def bind_values(self, values):
        self._values = values
        self.refresh()

    def refresh(self):
        if self._values is None:
            return
        values = self._values()
        self._procedure = selected_procedure(values, self.index)
        training = "Training and validation" if self._procedure.cross_validation else "Training"
        for editor in self.roles.values():
            with QSignalBlocker(editor):
                editor.setItemText(editor.findData("training"), training)
        roles = self.role_by_subject()
        counts = {role: sum(value == role for value in roles.values()) for role in ("training", "test", "not_selected", None)}
        show_test = self._procedure.independent_test or counts["test"] > 0
        show_left_out = counts["not_selected"] > 0
        # Excluding the sole training subject cannot yield a valid fit. Keep
        # recovery choices for an already excluded or unassigned subject.
        allow_leave_out = len(self.subjects) > 1 or any(role != "training" for role in roles.values())
        names = {"training": training, "test": "Independent testing", "not_selected": "Leave out", None: "Unassigned"}
        if hasattr(self, "fold_choice"):
            self.fold_choice.set_automatic_folds(counts["training"])
        with QSignalBlocker(self.move_role):
            previous = self.move_role.currentData()
            self.move_role.clear()
            for role in ("training", "test", "not_selected"):
                if (role != "test" or show_test) and (role != "not_selected" or allow_leave_out):
                    self.move_role.addItem(names[role], role)
            self.move_role.setCurrentIndex(max(0, self.move_role.findData(previous)))
        self._can_move = self.move_role.count() > 1
        self.move_hint.setVisible(self._can_move and not self._integrated_procedure)
        if self.cohort is not None:
            self.procedure.setText(LABELS[self._procedure.name])
            self.summary.setText(self._inventory_count(self.subjects))
        self.board.setToolTip("Drag a subject to another group, or select it and use Move to." if self._can_move else "")
        details = {
            "training": "",
            "test": "",
            "not_selected": "Not used for fitting or evaluation.",
            None: "Choose a role before fitting.",
        }
        for role, entries in self.lanes.items():
            self.lane_hosts[role].setVisible({"training": True, "test": show_test, "not_selected": show_left_out, None: counts[None] > 0}[role])
            self.lane_titles[role].setText(names[role])
            image_count = sum(len(self.subjects[subject]) for subject, assigned in roles.items() if assigned == role)
            self.lane_counts[role].setText(f"{counts[role]} {'subject' if counts[role] == 1 else 'subjects'} · {image_count} {'image' if image_count == 1 else 'images'}")
            single_training_group = role == "training" and not (show_test or show_left_out or counts[None])
            self.lane_counts[role].setVisible(not (single_training_group and self._integrated_procedure
                                                   and self._procedure.cross_validation))
            self.lane_help[role].setText(details[role])
            self.lane_help[role].setVisible(bool(details[role]))
        self._refresh_fold_preview(values, roles)
        selected_fold = self.fold_selector.currentData()
        material = (tuple(roles.items()), training, show_test, show_left_out,
                    tuple(self._fold_assignments.items()), selected_fold)
        if material != self._material:
            if self._integrated_procedure:
                self.detail.hide()
                self.detail.setParent(self)
            self._material = material
            for lane in self.lanes.values():
                while lane.cards.count():
                    widget = lane.cards.takeAt(0).widget()
                    if widget is not None:
                        widget.setParent(None)
                        widget.deleteLater()
            self._cards = {}
            self._fold_groups = []
            for subject, role in roles.items():
                card = SubjectCard(self, subject, self.lanes[role])
                self._cards[subject] = card
                if role != "training" or selected_fold is None:
                    self.lanes[role].cards.addWidget(card)
            if selected_fold is not None:
                lane = self.lanes["training"]
                for validation, title in ((True, "Validate"), (False, "Train")):
                    subjects = tuple(subject for subject, fold in self._fold_assignments.items()
                                     if (fold == selected_fold) == validation)
                    heading = _label(f"{title} in fold {selected_fold + 1} · {self._inventory_count(subjects)}", lane)
                    heading.setStyleSheet("font-weight: 600; color: #173d47;")
                    lane.cards.addWidget(heading)
                    self._fold_groups.append((heading, subjects))
                    for subject in subjects:
                        lane.cards.addWidget(self._cards[subject])
            for role, lane in self.lanes.items():
                if counts[role] == 0:
                    lane.cards.addWidget(_label("Drop subjects here" if self._can_move else "No subjects assigned", lane))
            self._filter_cards()
        self.automatic.setText("Assign automatically" if self._procedure.independent_test else "Assign all to training and validation" if self._procedure.cross_validation else "Assign all to training")
        can_assign = len(self.subjects) > 1 and (
            not self._integrated_procedure or self._procedure.independent_test or counts["training"] != len(roles))
        if self._integrated_procedure:
            group = self.lane_hosts["test" if self._procedure.independent_test else "training"]
            if group is not self.automatic_host.parentWidget():
                group.layout().insertWidget(2, self.automatic_host)
        self.automatic.setVisible(can_assign)
        self.automatic_host.setVisible(can_assign)
        if self._test_field is not None:
            self._test_host.setVisible(self._procedure.independent_test and len(self.subjects) > 1)
        if self._fold_host is not None:
            self._fold_host.setVisible(self._procedure.cross_validation)
        self.automatic.setToolTip("Uses the allocation seed in Advanced settings and replaces all roles shown on this board.")
        self.select_subject(self._selected)
        self._layout_lanes()
        self._size_lanes()
        error = self.validation_error()
        self.message.setText(error or "")
        self.message.setVisible(bool(error))

    def _inventory_count(self, subjects):
        count = len(subjects)
        images = sum(len(self.subjects[subject]) for subject in subjects)
        return f"{count} {'subject' if count == 1 else 'subjects'} · {images} {'image' if images == 1 else 'images'}"

    def _refresh_fold_preview(self, values, roles):
        """Preview the exact runtime split without collecting references or changing roles."""
        self._fold_assignments = {}
        error = ""
        if self._procedure.cross_validation:
            try:
                # Integer form editors expose draft text; submission decodes the
                # same strings to integers before calling the runtime owner.
                folds = values.get(self.prefix + "_cv_folds", 0)
                seed = values.get(self.prefix + "_seed", 0)
                folds = int(folds) if isinstance(folds, str) else folds
                seed = int(seed) if isinstance(seed, str) else seed
            except (TypeError, ValueError):
                error = "Enter whole-number subject folds and an allocation seed to inspect validation folds."
            else:
                try:
                    self._fold_assignments = subject_folds(
                        [subject for subject, role in roles.items() if role == "training"], folds, seed)
                except (TypeError, ValueError) as exc:
                    error = str(exc)
        with QSignalBlocker(self.fold_selector):
            previous = self.fold_selector.currentData()
            self.fold_selector.clear()
            if self._fold_assignments:
                self.fold_selector.addItem("All training subjects" if self._integrated_procedure else "All subjects · inspect folds", None)
                for fold in sorted(set(self._fold_assignments.values())):
                    subjects = tuple(subject for subject, assigned in self._fold_assignments.items() if assigned == fold)
                    inventory = self._inventory_count(subjects)
                    self.fold_selector.addItem(f"Fold {fold + 1} · {inventory}" if self._integrated_procedure
                                               else f"Fold {fold + 1} · validate {inventory}", fold)
                self.fold_selector.setCurrentIndex(max(0, self.fold_selector.findData(previous)))
        self.fold_status.setText(error)
        self.fold_status.setVisible(bool(self.fold_status.text()))
        if self._integrated_procedure:
            with QSignalBlocker(self.fold_tabs):
                size = self.fold_selector.count()
                if size != self.fold_tabs.count():
                    while self.fold_tabs.count():
                        self.fold_tabs.removeTab(self.fold_tabs.count() - 1)
                    for i in range(size):
                        self.fold_tabs.addTab(f"Fold {i}" if i else "All subjects")
                for i in range(1, size):
                    members = [s for s, fold in self._fold_assignments.items() if fold == i - 1]
                    self.fold_tabs.setTabToolTip(i, f"Validation · {self._inventory_count(members)}\n" + ", ".join(members))
                self.fold_tabs.setCurrentIndex(self.fold_selector.currentIndex())
            self.fold_tabs.setVisible(bool(self._fold_assignments))
        self.fold_selector.setVisible(bool(self._fold_assignments) and not self._integrated_procedure)
        self.fold_preview.setVisible(self._procedure.cross_validation)

    def role_by_subject(self):
        return {subject: editor.currentData() for subject, editor in self.roles.items()}

    def validation_error(self):
        if self._procedure is None:
            return None
        roles = tuple(self.role_by_subject().values())
        training = roles.count("training")
        testing = roles.count("test")
        if None in roles:
            return "Assign each subject a role, or move it to Leave out."
        if not training:
            return "Assign at least one subject to training."
        if self._procedure.cross_validation and training < 2:
            return "Cross-validation needs at least two subjects in Training and validation."
        if self._procedure.independent_test and not testing:
            return "Assign at least one subject to independent testing, or choose a procedure without independent testing."
        if not self._procedure.independent_test and testing:
            return "Independent-test subjects are assigned. Choose a procedure with independent testing, or change their roles."
        return None

    def select_subject(self, subject):
        self._selected = subject
        for value, card in self._cards.items():
            card.select(value == subject)
        count = len(self.subjects.get(subject, ()))
        self.selected.setText(f"{subject} · {count} {'image' if count == 1 else 'images'}" if subject
                              else "Select a subject to inspect its images.")
        role = self.role_by_subject().get(subject)
        fold = self._fold_assignments.get(subject)
        if fold is not None:
            count = len(set(self._fold_assignments.values())) - 1
            remaining = "the other fold" if count == 1 else f"the other {count} folds"
            self.selected_fold_role.setText(f"Validates in fold {fold + 1}; trains in {remaining} and the final refit.")
        elif role == "test":
            self.selected_fold_role.setText("Independent test: excluded from all validation folds and final fitting.")
        else:
            self.selected_fold_role.setText("")
        self.selected_fold_role.setVisible(bool(self._fold_assignments) and bool(self.selected_fold_role.text()))
        self.move_controls.setVisible(not self._integrated_procedure and self._can_move and subject is not None)
        self.move_button.setEnabled(subject is not None
            and self.role_by_subject().get(subject) != self.move_role.currentData())
        self._refresh_detail()
        if self._integrated_procedure:
            card = self._cards.get(subject)
            self.selected.hide()
            self.selected_fold_role.hide()
            if card is None:
                self.detail.hide()
            else:
                lane = self.lanes[role]
                if lane.cards.indexOf(self.detail) != lane.cards.indexOf(card) + 1:
                    lane.cards.removeWidget(self.detail)
                    lane.cards.insertWidget(lane.cards.indexOf(card) + 1, self.detail)
                self.detail.show()
            self._size_lanes()

    def set_saved_regions(self, summaries):
        """Display already-returned annotation regions for the offered images."""
        entries = {entry for values in self.subjects.values() for entry in values}
        self._saved_regions = {entry: summary for entry, summary in summaries.items() if entry in entries}
        self._refresh_detail()
        self._size_lanes()

    def _refresh_detail(self):
        names = _reference_names(self._values(), self.index, self.reference_names) if self._values else ()
        role = self.role_by_subject().get(self._selected)
        rows = []
        for entry in self.subjects.get(self._selected, ()):
            summary = self._saved_regions.get(entry)
            counts = (tuple((name, summary["class_region_counts"].get(name, 0)) for name in names)
                      if summary is not None else None)
            rows.append((entry, self.participants[entry]["sample_id"], counts))
        material = (self._selected, role, tuple(rows))
        if material == self._detail_material:
            return
        self._detail_material = material
        show_counts = self._show_reference_counts and any(counts is not None for _entry, _sample, counts in rows)
        self.image_heading.setVisible(bool(rows) and (show_counts or not self._integrated_procedure))
        self.annotation_heading.setVisible(show_counts)
        while self.image_rows.count():
            widget = self.image_rows.takeAt(0).widget()
            widget.setParent(None)
            widget.deleteLater()
        for entry, sample, counts in rows:
            row = QFrame(self.detail)
            row.setObjectName("subject-image-row")
            divider = "border-left: 1px solid #a6c6c0" if self._integrated_procedure else "border-top: 1px solid #e1e8e8"
            row.setStyleSheet("QFrame#subject-image-row { border: 0; " + divider + "; }")
            line = QGridLayout(row)
            line.setContentsMargins(10 if self._integrated_procedure else 0, 6, 0, 6)
            line.setHorizontalSpacing(16)
            name = _label(sample, row)
            if self._show_reference_counts and role in {"training", "test"}:
                name.setTextFormat(Qt.TextFormat.RichText)
                name.setText('<a style="color: #006b70;" href="select">' + escape(sample) + '</a>')
                name.setToolTip("Select this image for reference annotations.")
                name.linkActivated.connect(lambda _link, entry=entry: self.image_selected.emit(entry))
            name.setAccessibleName(sample)
            line.addWidget(name, 0, 0, Qt.AlignmentFlag.AlignTop)
            if show_counts:
                detail = _label(" · ".join(f"{name}: {count}" for name, count in counts)
                                if counts else "Not returned" if counts is None else "No class labels selected", row)
                line.addWidget(detail, 0, 1, Qt.AlignmentFlag.AlignTop)
            line.setColumnStretch(0, 1)
            line.setColumnStretch(1, 2 if show_counts else 0)
            self.image_rows.addWidget(row)

    def _list_destinations(self):
        self.group_menu.clear()
        current = self.role_by_subject().get(self._selected)
        for index in range(self.move_role.count()):
            role = self.move_role.itemData(index)
            if role != current:
                self.group_menu.addAction("Move to " + self.move_role.itemText(index)).setData(role)

    def _move_selected(self):
        if self._selected is not None and self.move_button.isEnabled():
            self._set_roles({self._selected: self.move_role.currentData()})

    def _set_roles(self, assignments):
        for subject, role in assignments.items():
            editor = self.roles[subject]
            with QSignalBlocker(editor):
                editor.setCurrentIndex(editor.findData(role))
        self.message.hide()
        self.refresh()
        self.changed.emit()

    def assign_automatically(self):
        if self._values is None:
            return
        values = self._values()
        procedure = selected_procedure(values, self.index)
        try:
            assignments = self._automatic_roles(values, procedure)
        except (TypeError, ValueError) as error:
            self.message.setText(str(error))
            self.message.show()
            return
        self._set_roles(assignments)

    def _automatic_roles(self, values, procedure):
        allocation = assign_subject_roles(
            entry_subjects={entry: subject for subject, entries in self.subjects.items() for entry in entries},
            validation_subjects=0,
            test_subjects=int(values.get(self.prefix + "_test_subjects") or 0) if procedure.independent_test else 0,
            seed=int(values[self.prefix + "_seed"]),
            evaluation_intent=procedure.evaluation_intent,
        )
        return {self.participants[entry]["subject_id"]: role for entry, role in allocation}

    def _size_lanes(self):
        fold_headings = {heading for heading, _subjects in self._fold_groups}
        resized = False
        for role, lane in self.lanes.items():
            if self.lane_hosts[role].isHidden():
                continue
            items = [lane.cards.itemAt(i).widget() for i in range(lane.cards.count())]
            height = sum((widget.heightForWidth(max(1, lane.viewport().width() - 6))
                          if widget in fold_headings else widget.sizeHint().height()) + 7
                         for widget in items if not widget.isHidden())
            if self._integrated_procedure:
                width = max(1, lane.viewport().width())
                height = max(lane.cards.sizeHint().height(), lane.cards.heightForWidth(width)) + 4
            required = min(480 if self._integrated_procedure else 260, max(64, height))
            if lane.minimumHeight() != required or lane.maximumHeight() != required:
                lane.setFixedHeight(required)
                self.lane_hosts[role].layout().invalidate()
                self.lane_hosts[role].updateGeometry()
                resized = True
        if resized:
            self.board_layout.invalidate()
            self.board.updateGeometry()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._layout_lanes()

    def _layout_lanes(self):
        if not hasattr(self, "lane_hosts"):
            return
        roles = tuple(role for role, host in self.lane_hosts.items() if not host.isHidden())
        columns = 2 if self.width() >= (730 if self._integrated_procedure else 660) and len(roles) > 1 else 1
        layout_key = (roles, columns)
        if getattr(self, "_lane_layout", None) == layout_key:
            return
        self._lane_layout = layout_key
        while self.board_layout.count():
            self.board_layout.takeAt(0)
        for index, role in enumerate(roles):
            # A selected fold adds and removes group headings at the same width.
            # Read its current height instead of reusing a cached QWidgetItemV2.
            item = QWidgetItem(self.lane_hosts[role])
            self.board_layout.addItem(item, index // columns, index % columns, 1, 1, Qt.AlignmentFlag.AlignTop)
        self.board_layout.setColumnStretch(0, 1)
        self.board_layout.setColumnStretch(1, 1 if columns == 2 else 0)

    def _filter_cards(self):
        query = self.search.text().strip().casefold()
        visible = []
        for subject, card in self._cards.items():
            names = (subject, *(self.participants[entry]["sample_id"] for entry in self.subjects[subject]))
            matches = not query or any(query in name.casefold() for name in names)
            card.setVisible(matches)
            if matches:
                visible.append(subject)
        for heading, subjects in self._fold_groups:
            heading.setVisible(any(subject in visible for subject in subjects))
        if self._selected not in visible:
            self.select_subject(None)
        self.search_status.setText(
            f"{len(visible)} of {len(self.subjects)} subjects shown" if visible
            else "No subjects or images match this search.")
        self.search_status.setVisible(bool(query))
        self._size_lanes()


class _ReferenceIdentity(QLabel):
    """Keep long source names readable through an ellipsis and full-name tooltip."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._full_text = ""
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

    def setText(self, text):
        self._full_text = text
        self.setToolTip(text)
        self.setAccessibleName(text)
        self._elide()
        self.updateGeometry()

    def _elide(self):
        super().setText(self.fontMetrics().elidedText(
            self._full_text, Qt.TextElideMode.ElideRight, max(0, self.contentsRect().width())))

    def sizeHint(self):
        return QSize(self.fontMetrics().horizontalAdvance(self._full_text) + 8,
                     self.fontMetrics().height() + 4)

    def minimumSizeHint(self):
        return QSize(0, self.sizeHint().height())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._elide()


class ReferencePlanSummary(QWidget):
    """Select a reference image, annotate it, and inspect returned class counts."""

    image_selected = Signal(str)
    annotation_requested = Signal()

    def __init__(self, allocations, values, parent=None):
        super().__init__(parent)
        self.setObjectName("reference-plan-summary")
        self.allocations = allocations
        self.selected_index = None
        for allocation in allocations.values():
            allocation.image_selected.connect(self.select_image)
        self._values = values
        self._entries = ()
        self._selected = None
        self._saved_regions = {}
        self._required_by_entry = {}
        self._scope_by_entry = {}
        self._test_entries = set()
        self._annotation_available = False
        from .training_evidence import configure_evidence_table
        self._fit_timer = QTimer(self)
        self._fit_timer.setSingleShot(True)
        self._fit_timer.timeout.connect(self._fit_tables)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        selector = QHBoxLayout()
        selector.setSpacing(12)
        selector.addWidget(_label("Image", self))
        from .reference_cell_annotations import ReferenceImageNavigator
        self.navigator = ReferenceImageNavigator(self)
        self.navigator.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        self.image = self.navigator.image
        self.image.setObjectName("reference-plan-image")
        self.image.setAccessibleName("Reference image to annotate")
        self.image.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.image.setMinimumContentsLength(20)
        self.image.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.image.currentIndexChanged.connect(self._select_image)
        selector.addWidget(self.navigator)
        self.image_name = _ReferenceIdentity(self)
        self.image_name.setObjectName("reference-selected-image")
        self.image_name.hide()
        selector.addWidget(self.image_name)
        selector.addStretch(1)
        self.annotate = ContentSizedButton("Annotate in QuPath", self)
        self.annotate.setObjectName("reference-annotate-image")
        self.annotate.setProperty("actionRole", "primary")
        self.annotate.setEnabled(False)
        self.annotate.clicked.connect(self.annotation_requested)
        selector.addWidget(self.annotate)
        layout.addLayout(selector)
        self.metadata = _label('', self)
        self.metadata.setObjectName('reference-selected-subject-role')
        self.metadata.setProperty('choiceHelp', False)
        self.metadata.setProperty('fieldHelp', True)
        layout.addWidget(self.metadata)
        self.scope = _label("", self)
        self.scope.setObjectName("reference-detection-area")
        self.scope.setProperty('choiceHelp', False)
        self.scope.setProperty('fieldHelp', True)
        layout.addWidget(self.scope)
        self.classes = _table(("Reference class", "Saved regions"), self, "reference-class-regions")
        self.classes.horizontalHeaderItem(0).setToolTip("Use these exact class names for reference annotations in QuPath.")
        self.classes.horizontalHeaderItem(1).setToolTip("Saved annotation counts for the selected image.")
        configure_evidence_table(self.classes)
        self.classes.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.classes.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.classes.horizontalHeader().sectionResized.connect(self._schedule_fit)
        layout.addWidget(self.classes)
        self.status = _label("", self)
        self.status.setObjectName("reference-coverage-status")
        self.status.setProperty('choiceHelp', False)
        self.status.setProperty('fieldHelp', True)
        layout.addWidget(self.status)
        self.availability = _label("", self)
        self.availability.setObjectName("reference-annotation-availability")
        self.availability.hide()
        layout.addWidget(self.availability)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._schedule_fit()

    def _schedule_fit(self, *_args):
        self._fit_timer.start(0)

    def _fit_tables(self):
        from .training_evidence import fit_evidence_table
        fit_evidence_table(self.classes)

    def set_annotation_available(self, available, reason=""):
        """The review host owns native launch availability, not the form."""
        self._annotation_available = bool(available)
        self.annotate.setEnabled(self._annotation_available and self._selected is not None)
        self.availability.setText(reason)
        self.availability.setVisible(bool(reason) and bool(self._entries))

    def refresh(self):
        values = self._values()
        rows = []
        self._required_by_entry = {}
        self._scope_by_entry = {}
        self._test_entries = set()
        for index, allocation in self.allocations.items():
            if self.selected_index is not None and index != self.selected_index:
                continue
            names = _reference_names(values, index, allocation.reference_names)
            scope = ("Detection area: " + ", ".join(allocation.permitted_parents)
                     if allocation.permitted_parents else "")
            if scope and len(self.allocations) > 1 and self.selected_index is None:
                scope = allocation.classifier_label + " · " + scope
            for subject, entries in allocation.subjects.items():
                role = allocation.role_by_subject().get(subject)
                if role not in {"training", "test"}:
                    continue
                title = ("Training and validation" if allocation._procedure.cross_validation else "Training") if role == "training" else "Independent test"
                if len(self.allocations) > 1 and self.selected_index is None:
                    title = allocation.classifier_label + ": " + title
                rows.extend((entry, allocation.participants[entry]["sample_id"], subject, title) for entry in entries)
                for entry in entries:
                    self._required_by_entry[entry] = tuple(dict.fromkeys((*self._required_by_entry.get(entry, ()), *names)))
                    self._scope_by_entry[entry] = tuple(dict.fromkeys((*self._scope_by_entry.get(entry, ()), scope)))
                    if role == "test":
                        self._test_entries.add(entry)
        merged = {}
        for entry, sample, subject, role in rows:
            if entry in merged:
                merged[entry][3] += "; " + role
            else:
                merged[entry] = [entry, sample, subject, role]
        rows = [tuple(row) for row in merged.values()]
        material = tuple(rows)
        if material != self._entries:
            self._entries = material
            selected = next((i for i, row in enumerate(rows) if row[0] == self._selected), 0)
            with QSignalBlocker(self.image):
                self.image.clear()
                for entry, sample, subject, role in rows:
                    self.image.addItem(sample, entry)
                self.image.setCurrentIndex(selected if rows else -1)
        self.image.setVisible(len(rows) > 1)
        self.navigator.setVisible(len(rows) > 1)
        self.navigator.refresh()
        self.image_name.setVisible(len(rows) <= 1)
        self._select_image()

    def set_saved_regions(self, summaries):
        self._saved_regions = dict(summaries)
        for allocation in self.allocations.values():
            allocation.set_saved_regions(self._saved_regions)
        self._show_selected_counts()

    def _show_selected_counts(self):
        summary = self._saved_regions.get(self._selected)
        counts = summary["class_region_counts"] if summary is not None else None
        names = self._required_by_entry.get(self._selected, ())
        self.classes.setRowCount(len(names))
        for row, name in enumerate(names):
            self.classes.setItem(row, 0, QTableWidgetItem(name))
            count = QTableWidgetItem(str(counts.get(name, 0)) if counts is not None else "—")
            count.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            count.setToolTip("Saved annotation count; training requirements are checked before fitting."
                             if counts is not None else "No count returned during this review.")
            self.classes.setItem(row, 1, count)
        self._schedule_fit()
        self.classes.setVisible(bool(names))
        if self._selected is None:
            self.status.setText("Assign subjects to training or independent testing to choose reference images.")
        elif summary is None:
            self.status.setText("Save and close QuPath to return the annotations.")
        else:
            self.status.setText("Annotations saved for this image.")

    def _select_image(self, *_args):
        row = self.image.currentIndex()
        previous = self._selected
        self._selected = self._entries[row][0] if 0 <= row < len(self._entries) else None
        if self._selected is not None:
            _entry, sample, subject, role = self._entries[row]
            self.image_name.setText(sample)
            distinguish_subject = sample != subject and len({entry[2] for entry in self._entries}) > 1
            self.metadata.setText(f"Subject: {subject} · {role}" if distinguish_subject else role)
            scope = [text for text in self._scope_by_entry.get(self._selected, ()) if text]
            context = "\n".join([f"Subject: {subject}", *scope])
            self.metadata.setToolTip(context)
            self.metadata.setAccessibleDescription(context)
            self.image.setToolTip("\n".join([sample, context]))
            self.image_name.setToolTip("\n".join([sample, context]))
            self.scope.setText("Independent-test images must be annotated without prediction feedback."
                               if self._selected in self._test_entries else "")
            self.annotate.setAccessibleName(f"Annotate {sample} in QuPath")
        else:
            self.image_name.setText("No reference images selected")
            self.image.setToolTip("")
        self.metadata.setVisible(self._selected is not None)
        self.scope.setVisible(self._selected is not None and bool(self.scope.text().strip()))
        self.annotate.setEnabled(self._annotation_available and self._selected is not None)
        self._show_selected_counts()
        if self._selected is not None and self._selected != previous:
            self.image_selected.emit(self._selected)

    def select_image(self, entry):
        row = next((i for i, value in enumerate(self._entries) if value[0] == entry), None)
        if row is not None:
            self.image.setCurrentIndex(row)
