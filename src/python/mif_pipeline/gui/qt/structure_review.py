"""Selected region measurements beside their saved comparisons."""
from PySide6.QtCore import Qt, QSignalBlocker, QEvent, QTimer
from PySide6.QtWidgets import QBoxLayout, QFrame, QFormLayout, QHBoxLayout, QGridLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget, QScrollArea, QPlainTextEdit
from .selection_option import SelectionOption


class StructureCriterionReview(QFrame):
    consumed_tables = frozenset({'semantic_scope', 'semantic_evidence'})

    def __init__(self, form, parent=None, *, image_review=None, controls_layout=None, subtype_editor=None, candidate_editor=None):
        super().__init__(parent)
        self.consumed_tables = frozenset(table.table_id for table in form.tables
            if table.table_id == 'semantic_scope' or table.table_id.startswith('semantic_evidence'))
        self.setObjectName('structure-criterion-review')
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        tables = {table.table_id: table for table in form.tables}
        scope = tables.get('semantic_scope')
        self._navigation = None
        self._candidate_editor = candidate_editor
        self._current_roi_id = form.context.get("resume_context", {}).get("roi_id")
        if scope is not None:
            values = {str(row.values[0]): str(row.values[1]) for row in scope.rows}
            header = QHBoxLayout()
            header.setSpacing(20)
            identity = QVBoxLayout()
            identity.setSpacing(4)
            title = QLabel(values.get('Structure', 'Review structure'), self)
            title.setObjectName('structure-review-name')
            title.setTextFormat(Qt.TextFormat.PlainText)
            title.setWordWrap(True)
            title.setStyleSheet('font-size:19px; font-weight:600; color:#174f53;')
            identity.addWidget(title)
            image = QLabel(values.get('Image', ''), self)
            image.setTextFormat(Qt.TextFormat.PlainText)
            image.setWordWrap(True)
            image.setProperty('choiceHelp', True)
            identity.addWidget(image)
            header.addLayout(identity, 1)
            progress = QVBoxLayout()
            progress.setSpacing(6)
            pending = form.context.get("resume_context", {}).get("review_candidates")
            progress_text = (f"{len(pending)} region{'s' if len(pending) != 1 else ''} remaining"
                             if pending else values.get('Region', 'Current region'))
            label = QLabel(progress_text, self)
            label.setObjectName('structure-review-progress')
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setStyleSheet('font-weight:600; color:#174f53;')
            progress.addWidget(label)
            header.addLayout(progress)
            layout.addLayout(header)
        if candidate_editor is not None:
            for row in range(controls_layout.rowCount()):
                item = controls_layout.itemAt(row, QFormLayout.ItemRole.FieldRole)
                wrapper = item.widget() if item is not None else None
                if wrapper is not None and (
                    wrapper is candidate_editor or wrapper.isAncestorOf(candidate_editor)
                    or isinstance(wrapper, QLabel) and wrapper.buddy() is candidate_editor
                ):
                    controls_layout.setRowVisible(row, False)
                    if wrapper is candidate_editor:
                        controls_layout.removeWidget(candidate_editor)
                    elif wrapper.layout() is not None:
                        wrapper.layout().removeWidget(candidate_editor)
            self._navigation = QHBoxLayout()
            self._navigation.setSpacing(8)
            label = QLabel("Region", self)
            label.setBuddy(candidate_editor)
            self._navigation.addWidget(label)
            self._navigation.addWidget(candidate_editor, 1)
            candidate_editor.setMinimumWidth(0)
            candidate_editor.setMaximumWidth(360)
            candidate_editor.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            candidate_editor.setToolTip("Pending regions, largest first. Opening a region records no decision.")
            layout.addLayout(self._navigation)
            candidate_editor.show()
        self._columns = None
        self._image_review = image_review
        self._scroll_viewport = None
        self._workspace_update_pending = False
        self._subtype_editor = subtype_editor
        self._subtype_buttons = {}
        self._evidence_panels = []
        measurements = QVBoxLayout()
        self._decision_layout = measurements
        measurements.setContentsMargins(0, 0, 0, 0)
        measurements.setSpacing(10)
        measurements.setAlignment(Qt.AlignmentFlag.AlignTop)
        if image_review is not None:
            self._columns = QBoxLayout(QBoxLayout.Direction.LeftToRight)
            self._columns.setSpacing(18)
            self._columns.addWidget(image_review, 1)
            if hasattr(image_review.view, 'height_changed'):
                image_review.view.height_changed.connect(self._schedule_workspace_size)
            measurement_panel = QFrame(self)
            measurement_panel.setObjectName('structure-review-decision-panel')
            measurement_panel.setStyleSheet('QFrame#structure-review-decision-panel { background:#ffffff; border:1px solid #d2ded9; border-radius:8px; }')
            measurements.setContentsMargins(14, 14, 14, 14)
            measurement_panel.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
            measurement_panel.setLayout(measurements)
            self._columns.addWidget(measurement_panel, 1)
            self._measurement_panel = measurement_panel
            layout.addLayout(self._columns)
        else:
            layout.addLayout(measurements)
        self._subtype_heading = None
        if subtype_editor is not None:
            decision = QFrame(self)
            decision.setObjectName("structure-subtype-decision")
            decision.setStyleSheet("""
                QFrame#structure-subtype-decision { background:transparent; border:0; }
                QFrame#structure-subtype-decision QFrame[selectionOption="true"] { background:transparent; border:1px solid transparent; border-radius:3px; }
                QFrame#structure-subtype-decision QFrame[selectionOption="true"][optionSelected="true"] { background:#e4f0ec; }
                QFrame#structure-subtype-decision QFrame[selectionOption="true"][optionFocused="true"] { border:1px dotted #006b70; }
                QFrame#structure-subtype-decision QFrame[uncertainChoice="true"][optionSelected="true"] { background:#eef2f1; }
            """)
            decision_layout = QVBoxLayout(decision)
            decision_layout.setContentsMargins(0, 16, 0, 4)
            decision_layout.setSpacing(6)
            label = QLabel("Assign subtype", decision)
            label.setProperty("panelHeading", True)
            decision_layout.addWidget(label)
            self._subtype_heading = decision
            measurements.addWidget(decision)
            self._subtype_choices = QWidget(decision)
            self._choice_grid = QGridLayout(self._subtype_choices)
            self._choice_grid.setContentsMargins(0, 0, 0, 0)
            self._choice_grid.setSpacing(6)
            self._subtype_choices.installEventFilter(self)
            self._subtype_options = {}
            self._subtype_rows = []
            self._subtype_columns = None
            self._fitting_choices = False
            decision_layout.addWidget(self._subtype_choices)
            for index in range(subtype_editor.count()):
                value = subtype_editor.itemData(index)
                if not value:
                    continue
                option = SelectionOption(subtype_editor.itemText(index), parent=decision)
                option.layout().setContentsMargins(8, 7, 8, 7)
                option.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Maximum)
                button = option.button
                button.setAccessibleName("Assign " + subtype_editor.itemText(index))
                button.clicked.connect(lambda _checked=False, selected=index: self._choose_subtype(selected))
                self._subtype_buttons[value] = button
                self._subtype_options[value] = option
                if value == 'cannot_assess':
                    option.setProperty('uncertainChoice', True)
                    button.setAccessibleName('Cannot assess subtype')
                    button.setAccessibleDescription('Accepting keeps this region with its subtype undetermined.')
                    option.setToolTip(button.accessibleDescription())
                self._subtype_rows.append(option)
                self._choice_grid.addWidget(option, len(self._subtype_rows) - 1, 0)
            self._fit_subtype_choices()
            for row in range(controls_layout.rowCount()):
                item = controls_layout.itemAt(row, QFormLayout.ItemRole.FieldRole)
                wrapper = item.widget() if item is not None else None
                if wrapper is not None and (
                    wrapper is subtype_editor or wrapper.isAncestorOf(subtype_editor)
                    or isinstance(wrapper, QLabel) and wrapper.buddy() is subtype_editor
                ):
                    controls_layout.setRowVisible(row, False)
            self._measurement_hint = QLabel(self)
            self._measurement_hint.setWordWrap(True)
            self._measurement_hint.setProperty("choiceHelp", True)
            measurements.addWidget(self._measurement_hint)
        for evidence in form.tables:
            if not evidence.table_id.startswith('semantic_evidence') or not evidence.rows:
                continue
            title = QLabel(('Review measurements' if evidence.table_id == 'semantic_evidence:0'
                            else 'Subtype measurements') if subtype_editor is not None
                           else evidence.title or 'Measurements to review', self)
            title.setProperty('panelHeading', True)
            view = QFrame(self)
            view.setObjectName('structure-criterion-results')
            view.installEventFilter(self)
            view.setAccessibleName(evidence.title or title.text())
            rows = QGridLayout(view)
            rows.setContentsMargins(0, 0, 0, 0)
            rows.setHorizontalSpacing(14)
            rows.setVerticalSpacing(7)
            has_threshold = any(str(row.values[2]).strip() not in {'', '—', 'None'} for row in evidence.rows)
            has_status = has_threshold or any(row.values[3] not in {'', None, '—', 'Measured'} for row in evidence.rows)
            has_summary = any(any(part in {'Mean across member cells', 'Median across member cells'}
                                  for part in str(row.values[4] or '').split('; '))
                              for row in evidence.rows)
            columns = [(0, "Measurement")]
            if has_summary:
                columns.append((4, "Summary"))
            columns.append((1, "Value"))
            if has_threshold or has_status:
                columns.append((2, "Comparison"))
            for column, (_source, text) in enumerate(columns):
                label = QLabel(text, view)
                label.setProperty('choiceHelp', True)
                label.setStyleSheet('font-weight:600; color:#47676a; padding:0;')
                label.setAlignment(Qt.AlignmentFlag.AlignLeft)
                rows.addWidget(label, 0, column)
            table_row = 1
            for index, row in enumerate(evidence.rows):
                name, observed, threshold, result, details = row.values
                detail_parts = str(details).split('; ') if details else []
                summaries = {'Mean across member cells': 'Mean',
                             'Median across member cells': 'Median'}
                summary = next((part for part in detail_parts if part in summaries), None)
                if summary is not None:
                    detail_parts.remove(summary)
                comparison = str(threshold) if threshold and str(threshold) not in {'—', 'None'} else '—'
                if result not in {'', None, '—', 'Measured', 'Met', 'Not met'}:
                    comparison = (comparison + '\n' if comparison != '—' else '') + str(result)
                comparison_color = {'Not met': '#8b512c', 'Unavailable': '#8b512c', 'Met': '#006b64'}.get(result, '#244b50')
                values = (name, observed, comparison, result or '—', summaries.get(summary, ''))
                for column, (source, _heading) in enumerate(columns):
                    text = values[source]
                    label = QLabel(str(text), view)
                    label.setTextFormat(Qt.TextFormat.PlainText)
                    label.setWordWrap(source == 0 or len(str(text)) > 24)
                    label.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
                    label.setMinimumWidth(0)
                    label.setStyleSheet('color:' + (comparison_color if source == 2 else '#244b50') + ';'
                                       + ('font-weight:600;' if source == 1 else ''))
                    if source == 2:
                        description = ' · '.join(str(part) for part in (name, observed, threshold, result) if part)
                        label.setToolTip(description)
                        label.setAccessibleDescription(description)
                    if source == 4 and summary is not None:
                        label.setToolTip(summary)
                    rows.addWidget(label, table_row, column)
                table_row += 1
                if detail_parts:
                    detail = QLabel('; '.join(detail_parts), view)
                    detail.setTextFormat(Qt.TextFormat.PlainText)
                    detail.setWordWrap(True)
                    detail.setProperty('choiceHelp', True)
                    rows.addWidget(detail, table_row, 0, 1, len(columns))
                    table_row += 1
                if index < len(evidence.rows) - 1:
                    divider = QFrame(view)
                    divider.setFixedHeight(1)
                    divider.setStyleSheet('background:#e4ece8; border:0;')
                    rows.addWidget(divider, table_row, 0, 1, len(columns))
                    table_row += 1
            for column, (source, _heading) in enumerate(columns):
                rows.setColumnStretch(column, 5 if source == 0 else 2)
            if evidence.table_id == 'semantic_evidence:0' and self._subtype_heading is not None:
                position = measurements.indexOf(self._subtype_heading)
                measurements.insertWidget(position, title)
                measurements.insertWidget(position + 1, view)
            else:
                measurements.addWidget(title)
                measurements.addWidget(view)
            self._evidence_panels.append((evidence.table_id, title, view))
        self._observations = []
        if controls_layout is not None:
            controls_layout.setContentsMargins(0, 0, 0, 0)
            controls_layout.setAlignment(Qt.AlignmentFlag(0))
            # Give the observation the remaining panel height, so a retained
            # explanation is readable beside its measurements and actions.
            for row in range(controls_layout.rowCount()):
                item = controls_layout.itemAt(row, QFormLayout.ItemRole.FieldRole)
                wrapper = item.widget() if item is not None else None
                if wrapper is not None:
                    for editor in wrapper.findChildren(QPlainTextEdit):
                        editor.setMinimumHeight(editor.fontMetrics().lineSpacing() * 4 + 28)
                        editor.setMaximumHeight(16777215)
                        editor.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Ignored)
                        wrapper.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
                        if isinstance(wrapper.layout(), QBoxLayout):
                            wrapper.layout().setStretch(wrapper.layout().indexOf(editor), 1)
                        self._observations.append(editor)
            measurements.addSpacing(4)
            measurements.addLayout(controls_layout, 1)
        self._decision_hint = QLabel('', self)
        self._decision_hint.setObjectName('structure-review-decision-hint')
        self._decision_hint.setWordWrap(True)
        self._decision_hint.setProperty('choiceHelp', True)
        self._accept_button = None
        if subtype_editor is not None:
            self._subtypes = tuple(form.context.get("resume_context", {}).get("declared_subtypes", ()))
            subtype_editor.currentIndexChanged.connect(self._show_subtype)
            self._show_subtype()

    def _choose_subtype(self, index):
        self._subtype_editor.setCurrentIndex(index)
        self._show_subtype()

    def _show_subtype(self, *_):
        """Show the measurements attached to the tentative subtype selection."""
        selected = self._subtype_editor.currentData()
        for value, button in self._subtype_buttons.items():
            with QSignalBlocker(button):
                button.setChecked(value == selected)
            self._subtype_options[value]._state("optionSelected", value == selected)
        for table_id, title, view in self._evidence_panels:
            number = int(table_id.split(":")[-1])
            visible = number == 0 or (number <= len(self._subtypes) and
                selected == "subtype:" + self._subtypes[number - 1])
            title.setVisible(visible)
            view.setVisible(visible)
        self._measurement_hint.setText("Accepting keeps this region with its subtype undetermined.")
        self._measurement_hint.setVisible(selected == 'cannot_assess')
        if self._accept_button is not None:
            self._accept_button.setEnabled(bool(selected))
        self._decision_hint.setText('Choose a subtype or Cannot assess before accepting.' if not selected else '')
        self._decision_hint.setVisible(not selected)
        self.updateGeometry()
        self._schedule_workspace_size()

    def add_decision_actions(self, actions):
        """Place the original form actions beside the evidence they save."""
        self._decision_layout.addWidget(self._decision_hint)
        self._decision_layout.addLayout(actions)
        for index in range(actions.count() - 1, -1, -1):
            widget = actions.itemAt(index).widget()
            if (widget is not None and self._navigation is not None
                    and widget.objectName().endswith('-action-select_semantic_region')):
                actions.removeWidget(widget)
                self._navigation.addWidget(widget)
                widget.setProperty('actionRole', 'secondary')
                widget.style().unpolish(widget)
                widget.style().polish(widget)
                update = lambda *_args, button=widget: button.setEnabled(
                    bool(self._candidate_editor.currentData()) and
                    self._candidate_editor.currentData() != self._current_roi_id)
                self._candidate_editor.currentIndexChanged.connect(update)
                update()
        if self._navigation is not None:
            self._navigation.addStretch(1)
        for index in range(actions.count()):
            widget = actions.itemAt(index).widget()
            if widget is not None and widget.objectName().endswith('-action-accept_semantic_region'):
                self._accept_button = widget
                widget.setProperty('actionRole', 'primary')
                widget.style().unpolish(widget)
                widget.style().polish(widget)
            elif widget is not None and widget.objectName().endswith('-action-reject_semantic_region'):
                widget.setProperty('actionRole', 'secondary')
                widget.style().unpolish(widget)
                widget.style().polish(widget)
        if self._subtype_editor is not None:
            self._show_subtype()
        else:
            self._decision_hint.hide()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._columns is not None:
            direction = (QBoxLayout.Direction.LeftToRight if event.size().width() >= 1000
                         else QBoxLayout.Direction.TopToBottom)
            if self._columns.direction() != direction:
                self._columns.setDirection(direction)
        if self._subtype_editor is not None:
            self._fit_subtype_choices()
        self._schedule_workspace_size()

    def showEvent(self, event):
        super().showEvent(event)
        parent = self.parentWidget()
        while parent is not None and not isinstance(parent, QScrollArea):
            parent = parent.parentWidget()
        if parent is not None and parent.viewport() is not self._scroll_viewport:
            if self._scroll_viewport is not None:
                self._scroll_viewport.removeEventFilter(self)
            self._scroll_viewport = parent.viewport()
            self._scroll_viewport.installEventFilter(self)
        self._schedule_workspace_size()

    def _schedule_workspace_size(self, *_):
        if not self._workspace_update_pending:
            self._workspace_update_pending = True
            QTimer.singleShot(0, self, self._size_workspace)

    def _size_workspace(self):
        """Keep the image and decision actions useful in the actual review viewport."""
        self._workspace_update_pending = False
        if not self.isVisible() or self._image_review is None:
            return
        # The enclosing scroll host can otherwise compress height-for-width
        # labels to their unconstrained two-line size hint in a multi-column row.
        # Resolve the actual row height after column widths are assigned.
        for _identity, _title, evidence in self._evidence_panels:
            if not evidence.isVisible():
                continue
            for label in evidence.findChildren(QLabel):
                if label.wordWrap():
                    label.setMinimumHeight(0)
                    label.setMaximumHeight(16777215)
                    label.setFixedHeight(max(0, label.heightForWidth(label.width())))
            evidence.layout().invalidate()
            evidence.setFixedHeight(evidence.layout().minimumSize().height())
        view = self._image_review.view
        stacked = self._columns.direction() == QBoxLayout.Direction.TopToBottom
        for editor in self._observations:
            editor.setMaximumHeight(editor.minimumHeight() if stacked else 16777215)
        if stacked:
            # Stacked reviews use the host's ordinary vertical scroll; avoid a
            # full-height image consuming the entire first screen.
            height = 320
        elif self._scroll_viewport is not None:
            review_height = self.layout().totalHeightForWidth(self.width())
            if review_height < 0:
                review_height = self.layout().sizeHint().height()
            scroll = self._scroll_viewport.parentWidget()
            content = scroll.widget()
            content_height = content.layout().totalHeightForWidth(content.width())
            if content_height < 0:
                content_height = content.layout().sizeHint().height()
            other_height = max(0, content_height - review_height)
            header_height = self._columns.geometry().top() - self.layout().contentsMargins().top()
            image_chrome = self._image_review.layout().sizeHint().height() - view.height()
            height = max(320, self._scroll_viewport.height() - other_height - header_height - image_chrome - 4)
        else:
            height = 480
        if view.height() != height:
            view.setFixedHeight(height)

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.Resize and any(watched is panel for _key, _title, panel in self._evidence_panels):
            self._schedule_workspace_size()
        if watched is self._scroll_viewport and event.type() == QEvent.Type.Resize:
            self._schedule_workspace_size()
        if watched is getattr(self, '_subtype_choices', None) and event.type() == QEvent.Type.Resize:
            self._fit_subtype_choices()
        return super().eventFilter(watched, event)

    def _fit_subtype_choices(self):
        if self._fitting_choices:
            return
        self._fitting_choices = True
        try:
            columns = 1
            if columns != self._subtype_columns:
                self._subtype_columns = columns
                for option in self._subtype_rows:
                    self._choice_grid.removeWidget(option)
                for index, option in enumerate(self._subtype_rows):
                    self._choice_grid.addWidget(option, index // columns, index % columns)
                for column in range(2):
                    self._choice_grid.setColumnStretch(column, 1 if column < columns else 0)
        finally:
            self._fitting_choices = False
