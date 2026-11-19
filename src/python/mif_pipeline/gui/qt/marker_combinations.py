"""Edit ordered phenotype rules without changing marker or threshold semantics."""

from PySide6.QtCore import QSignalBlocker, Qt, Signal, QTimer, QSize, QEvent
from PySide6.QtGui import QTextOption
from PySide6.QtWidgets import (
    QAbstractItemView, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QSizePolicy, QVBoxLayout, QWidget, QScrollArea, QFrame,
    QMenu, QListWidgetItem, QSpinBox, QToolButton, QPlainTextEdit, QWidgetAction,
)

from ...orchestration.batch_phenotyping.marker_combinations import UNMATCHED_PHENOTYPE
from .researcher_widgets import ContentSizedButton
from .wrapping_layout import WrappingLayout


class MarkerExpression(QFrame):
    """Edit the selected rule's required marker states in a wrapping expression."""

    state_changed = Signal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('marker-rule-expression')
        self.setStyleSheet('QFrame#marker-rule-expression { background:#e7f1ee; border:1px solid #c5dbd4; border-radius:8px; }')
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        self.setContentsMargins(12, 12, 12, 12)
        self._flow = WrappingLayout(self)
        self._parts = []
        self.buttons = {}
        self._text = ''
        self._arrange_timer = QTimer(self)
        self._arrange_timer.setSingleShot(True)
        self._arrange_timer.timeout.connect(self._arrange)

    def text(self):
        return self._text

    def set_rule(self, row):
        for part in self._parts:
            self._flow.removeWidget(part)
            part.hide()
            part.deleteLater()
        self._parts = []
        self.buttons = {}
        conditions = [(marker, state) for marker, state in row['marker_states'].items() if state != 'not_used']
        self._text = ' AND '.join(marker + ('+' if state == 'positive' else '−') for marker, state in conditions)
        self.setAccessibleName((self._text + ' assigns ' + row['phenotype_id']) if conditions else 'Choose marker conditions')
        if not conditions:
            self._text = 'Choose at least one positive or negative marker.'
            label = QLabel(self._text, self)
            label.setWordWrap(True)
            self._parts.append(label)
        for index, (marker, state) in enumerate(conditions):
            part = QWidget(self)
            line = QHBoxLayout(part)
            line.setContentsMargins(0, 0, 0, 0)
            line.setSpacing(8)
            if index:
                line.addWidget(QLabel('AND', part))
            button = ContentSizedButton(marker + (' +' if state == 'positive' else ' −'), part)
            button.setAccessibleName(marker + ': ' + state + '; change condition')
            button.setStyleSheet('QPushButton { background:#f6faf8; border:1px solid #99bbb2; border-radius:6px; padding:7px 10px; color:#005d62; font-weight:600; } QPushButton:hover { background:#d5e9e2; }')
            menu = QMenu(button)
            for value, title in (('positive', 'Positive +'), ('negative', 'Negative −'), ('not_used', 'Remove condition')):
                action = menu.addAction(title)
                action.setCheckable(value != 'not_used')
                action.setChecked(value == state)
                action.triggered.connect(lambda _checked=False, m=marker, v=value: self.state_changed.emit(m, v))
            button.setMenu(menu)
            line.addWidget(button)
            self.buttons[marker] = button
            self._parts.append(part)
        for part in self._parts:
            self._flow.addWidget(part)
        self._arrange()
        self._arrange_timer.start(0)

    def _arrange(self):
        margins = self.contentsMargins()
        available = max(1, self.width() - margins.left() - margins.right())
        self.setMinimumHeight(self._flow.heightForWidth(available) + margins.top() + margins.bottom())
        self.updateGeometry()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._arrange_timer.start(0)


class _MarkerConditionPicker(QMenu):
    """Choose an available marker and its sign without a permanent field list."""

    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        content = QWidget(self)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(12, 10, 12, 10)
        self.search = QLineEdit(content)
        self.search.setPlaceholderText('Find a marker…')
        self.search.setAccessibleName('Find a marker condition')
        layout.addWidget(self.search)
        scroll = QScrollArea(content)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        body = QWidget(scroll)
        rows = QVBoxLayout(body)
        rows.setContentsMargins(0, 0, 0, 0)
        rows.setSpacing(4)
        self.rows = {}
        for marker in owner.markers:
            row = QWidget(body)
            line = QHBoxLayout(row)
            line.setContentsMargins(4, 3, 4, 3)
            label = QLabel(marker, row)
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setWordWrap(True)
            line.addWidget(label, 1)
            for state, symbol in (('positive', '+'), ('negative', '−')):
                button = QToolButton(row)
                button.setText(symbol)
                button.setAccessibleName(marker + ': ' + state)
                button.setToolTip('Require ' + marker + ' ' + state)
                button.setMinimumSize(36, 32)
                button.clicked.connect(lambda _checked=False, m=marker, v=state: self.choose(m, v))
                line.addWidget(button)
            rows.addWidget(row)
            self.rows[marker] = row
        rows.addStretch()
        scroll.setWidget(body)
        scroll.setMinimumSize(280, min(330, 44 * max(2, len(owner.markers)) + 8))
        layout.addWidget(scroll)
        action = QWidgetAction(self)
        action.setDefaultWidget(content)
        self.addAction(action)
        self.search.textChanged.connect(self.filter)
        self.aboutToShow.connect(self.prepare)

    def prepare(self):
        self.search.clear()
        self.filter()
        QTimer.singleShot(0, self.search.setFocus)

    def filter(self, *_):
        index = self.owner.rules.currentRow()
        states = self.owner._rules[index]['marker_states'] if index >= 0 else {}
        query = self.search.text().casefold()
        for marker, row in self.rows.items():
            row.setVisible(states.get(marker) == 'not_used' and query in marker.casefold())

    def choose(self, marker, state):
        self.owner._change_condition(marker, state)
        self.hide()


class _PhenotypeName(QPlainTextEdit):
    """An inline name that shows every wrapped line and commits as one label."""

    editingFinished = Signal()
    height_changed = Signal()

    def __init__(self, text, parent):
        super().__init__(parent)
        self._saved = text
        self.setPlainText(text)
        self.setTabChangesFocus(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        option = self.document().defaultTextOption()
        option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.document().setDefaultTextOption(option)
        self._fit_timer = QTimer(self)
        self._fit_timer.setSingleShot(True)
        self._fit_timer.timeout.connect(self._fit_name)
        self.textChanged.connect(lambda: self._fit_timer.start(0))

    def text(self):
        return self.toPlainText()

    def setText(self, text):
        self._saved = text
        if self.toPlainText() != text:
            self.setPlainText(text)

    def setCursorPosition(self, position):
        cursor = self.textCursor()
        cursor.setPosition(position)
        self.setTextCursor(cursor)

    def cursorPosition(self):
        return self.textCursor().position()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_timer.start(0)

    def _fit_name(self):
        lines = max(1, self.document().documentLayout().documentSize().height())
        height = round(lines * self.fontMetrics().lineSpacing() + 2 * self.document().documentMargin() + 10)
        if self.height() != height:
            self.setFixedHeight(height)
            self.height_changed.emit()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.clearFocus()
        elif event.key() == Qt.Key.Key_Escape:
            self.setText(self._saved)
            self.clearFocus()
        else:
            super().keyPressEvent(event)

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        self.editingFinished.emit()

    def insertFromMimeData(self, source):
        self.insertPlainText(' '.join(source.text().split()))


class MarkerRuleRow(QFrame):
    """Edit a phenotype's name and position within its ordered row."""

    def __init__(self, owner, item, row, position):
        super().__init__(owner.rules)
        self.owner, self.item, self.row = owner, item, row
        self.setObjectName('phenotype-order-row')
        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(5)
        handle = QLabel('⋮⋮', self)
        handle.setStyleSheet('color:#688883; font-size:16px;')
        handle.setToolTip('Drag to change assignment order')
        handle.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout.addWidget(handle)
        self.position = QSpinBox(self)
        self.position.setRange(1, len(owner._rules))
        self.position.setValue(position)
        self.position.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        self.position.setFixedWidth(34)
        self.position.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.position.setAccessibleName('Assignment position for ' + row['phenotype_id'])
        self.position.setToolTip('Assignment position')
        self.position.setStyleSheet('QSpinBox { background:#e0eeea; border:1px solid #b6d0c8; border-radius:5px; padding:5px 2px; color:#005e64; }')
        layout.addWidget(self.position)
        self.name = _PhenotypeName(row['phenotype_id'], self)
        self.name.setCursorPosition(0)
        self.name.setMinimumWidth(80)
        self.name.setAccessibleName('Phenotype name')
        self.name.setToolTip(row['phenotype_id'])
        self.name.setStyleSheet('QPlainTextEdit { background:transparent; border:1px solid transparent; border-radius:4px; padding:3px 0px; font-weight:600; color:#164b50; } QPlainTextEdit:hover { border-color:#b6d0c8; } QPlainTextEdit:focus { background:#f7fbf9; border-color:#087f80; }')
        self.name.height_changed.connect(owner._fit_rules)
        layout.addWidget(self.name, 1)
        self.remove = QToolButton(self)
        self.remove.setText('×')
        self.remove.setAccessibleName('Remove ' + row['phenotype_id'])
        self.remove.setToolTip('Remove phenotype')
        self.remove.setStyleSheet('QToolButton { background:transparent; border:0; color:#69847e; font-size:19px; padding:3px; } QToolButton:hover { background:#f2e5e2; color:#8b4034; border-radius:4px; }')
        layout.addWidget(self.remove)
        self.name.installEventFilter(self)
        self.position.installEventFilter(self)
        self.name.editingFinished.connect(self._rename)
        self.position.editingFinished.connect(self._reposition)
        self.remove.clicked.connect(self._remove)

    def eventFilter(self, watched, event):
        if watched is self.name and event.type() == QEvent.Type.FocusOut:
            self.name.setCursorPosition(0)
        if (event.type() == QEvent.Type.FocusIn
                and not self.owner._refreshing
                and not self.owner._updating_rule):
            self.owner.rules.setCurrentItem(self.item)
        return super().eventFilter(watched, event)

    def _rename(self):
        if self.row not in self.owner._rules:
            return
        try:
            name = self.owner._name(self.name.text(), replacing=self.owner._rules.index(self.row))
        except ValueError as exc:
            self.owner.error.setText(str(exc)); self.owner.error.show()
            self.name.setText(self.row['phenotype_id'])
            return
        self.name.setText(name)
        if not self.name.hasFocus():
            self.name.setCursorPosition(0)
        self.owner.error.hide()
        if name == self.row['phenotype_id']:
            return
        self.row['phenotype_id'] = name
        self.name.setToolTip(name)
        self.item.setData(Qt.ItemDataRole.AccessibleTextRole, name)
        self.remove.setAccessibleName('Remove ' + name)
        self.position.setAccessibleName('Assignment position for ' + name)
        self.owner.error.hide()
        self.owner._show_rule(self.owner._rules.index(self.row))
        self.owner.changed.emit()

    def _reposition(self):
        if self.row in self.owner._rules:
            source = self.owner._rules.index(self.row)
            target = self.position.value() - 1
            if source != target:
                self.owner.move_rule_to(source, target)

    def detach(self):
        # A focused editor emits editingFinished when its row is removed.
        for field in (self.name, self.position):
            field.blockSignals(True)
            field.removeEventFilter(self)

    def _remove(self):
        if self.row in self.owner._rules:
            self.owner.rules.setCurrentItem(self.item)
            self.owner.remove_rule()


class MarkerCombinationEditor(QWidget):
    changed = Signal()
    page_changed = Signal()

    def __init__(self, markers, parent=None):
        super().__init__(parent)
        self.markers = tuple(markers)
        self._rules = []
        self._refreshing = self._updating_rule = False
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        hint = QLabel('Rules run from top to bottom. Each cell receives the first matching phenotype.', self)
        hint.setWordWrap(True)
        layout.addWidget(hint)
        body_host = QWidget(self)
        body_host.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        body = QHBoxLayout(body_host)
        body.setContentsMargins(0, 0, 0, 0)
        body.setAlignment(Qt.AlignmentFlag.AlignTop)
        listing = QVBoxLayout()
        listing.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.rules = QListWidget(self)
        self.rules.setObjectName('marker-ordered-rules')
        self.rules.setAccessibleName('Phenotypes in assignment order')
        self.rules.setWordWrap(True)
        self.rules.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.rules.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.rules.setStyleSheet('''
            QListWidget { background:#f2f5f4; border:1px solid #d0dedd;
                border-radius:8px; padding:4px; outline:none; }
            QListWidget::item { background:#edf3f1; color:#173d47;
                border:1px solid #d0dedd; border-radius:6px;
                padding:0px; margin-bottom:6px; }
            QListWidget::item:selected { background:#d4eae5; color:#005d62;
                border:1px solid #006b70; }
            QListWidget::item:hover { background:#e1eeea; }
        ''')
        self.rules.setMinimumWidth(280)
        self.rules.setMinimumHeight(170)
        self.rules.setMaximumHeight(340)
        listing.addWidget(self.rules)
        self.rules.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.rules.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.rules.setDragEnabled(True)
        self.rules.setAcceptDrops(True)
        self.rules.setDropIndicatorShown(True)
        self.rules.model().rowsMoved.connect(self._rows_moved)
        add = ContentSizedButton('+ Add phenotype', self)
        add.clicked.connect(self._add_inline)
        self.buttons = {'add': add}
        listing.addWidget(add, alignment=Qt.AlignmentFlag.AlignLeft)
        body.addLayout(listing, 2)
        self.detail = QWidget(self)
        detail = QVBoxLayout(self.detail)
        detail.setContentsMargins(12, 0, 0, 0)
        detail.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.expression = MarkerExpression(self.detail)
        self.expression.state_changed.connect(self._change_condition)
        detail.addWidget(self.expression)
        self.add_condition = ContentSizedButton('+ Add condition', self.detail)
        self.add_condition.setObjectName('marker-add-condition')
        self.condition_picker = _MarkerConditionPicker(self)
        self.add_condition.setMenu(self.condition_picker)
        detail.addWidget(self.add_condition, alignment=Qt.AlignmentFlag.AlignLeft)
        body.addWidget(self.detail, 3)
        layout.addWidget(body_host)
        self.error = QLabel(self)
        self.error.setWordWrap(True)
        self.error.setTextFormat(Qt.TextFormat.PlainText)
        self.error.hide()
        layout.addWidget(self.error)
        from .marker_assignment_preview import MarkerAssignmentPreview
        self.feedback = MarkerAssignmentPreview(self)
        self.feedback.patterns.selection_hint = 'Select Next: review assignments to inspect these cells in tissue.'
        self.feedback.hide()
        layout.addWidget(self.feedback)
        self.changed.connect(self.feedback.invalidate)
        self.rules.currentRowChanged.connect(self._show_rule)
        self._refresh(-1)
        self._editing_widgets = [layout.itemAt(index).widget() for index in range(layout.count())]
        from .marker_assignment_review import MarkerAssignmentReview
        self.review = MarkerAssignmentReview(self)
        self.review.hide()
        layout.addWidget(self.review)
        self.feedback.result_available.connect(self.review.set_result)
        self.feedback.failed.connect(self.review.set_error)
        self.review.preview_requested.connect(self.feedback.update_requested)
        self.reviewing = False

    def pattern_page_options(self):
        panel = self.review if self.reviewing else self.feedback
        return panel._page_request

    def set_preview_busy(self):
        if self.pattern_page_options() is None:
            self.feedback.set_busy()
        else:
            (self.review if self.reviewing else self.feedback).patterns.set_page_busy(True)

    def set_preview_result(self, result):
        if 'pattern_page' in result:
            (self.review if self.reviewing else self.feedback).set_pattern_page(result)
        else:
            self.feedback.set_result(result)

    def set_preview_error(self, message):
        if self.pattern_page_options() is None:
            self.feedback.set_error(message)
        else:
            panel = self.review if self.reviewing else self.feedback
            panel._page_request = None
            panel.patterns.page_failed(message)

    def start_review(self, definition):
        self.review.configure(definition)
        self.set_reviewing(True)
        self.feedback.update_requested.emit()

    def set_reviewing(self, reviewing):
        self.reviewing = reviewing
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Preferred if reviewing else QSizePolicy.Policy.Maximum)
        for widget in self._editing_widgets:
            if widget is not None:
                widget.setVisible(not reviewing)
        self.error.hide()
        self.review.setVisible(reviewing)
        if not reviewing:
            self.review.revision += 1
            self.review._timer.stop()
            self._show_rule(self.rules.currentRow())
        self.page_changed.emit()

    def _fit_rules(self):
        height = 12
        for index in range(self.rules.count()):
            item = self.rules.item(index)
            widget = self.rules.itemWidget(item)
            row_height = max(60, widget.sizeHint().height())
            item.setSizeHint(QSize(0, row_height))
            height += row_height + 6
        self.rules.setFixedHeight(min(340, max(84, height)))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_rules()

    def set_value(self, names, rows):
        states = {row['phenotype_id']: row['marker_states'] for row in rows}
        self._rules = [{'phenotype_id': name, 'marker_states': {
            marker: states.get(name, {}).get(marker, 'not_used') for marker in self.markers}}
            for name in names]
        self._refresh(0 if names else -1)

    def value(self):
        return {'marker_composite_names': [row['phenotype_id'] for row in self._rules],
                'marker_composite_states': [{'phenotype_id': row['phenotype_id'],
                    'marker_states': dict(row['marker_states'])} for row in self._rules]}

    def _refresh(self, index):
        self._refreshing = True
        try:
            with QSignalBlocker(self.rules):
                for i in range(self.rules.count()):
                    self.rules.itemWidget(self.rules.item(i)).detach()
                self.rules.clear()
                for order, row in enumerate(self._rules):
                    item = QListWidgetItem(self.rules)
                    item.setData(Qt.ItemDataRole.UserRole, order)
                    item.setData(Qt.ItemDataRole.AccessibleTextRole, row['phenotype_id'])
                    widget = MarkerRuleRow(self, item, row, order + 1)
                    self.rules.setItemWidget(item, widget)
                self.rules.setCurrentRow(index)
            self._show_rule(index)
            self._fit_rules()
            self.changed.emit()
        finally:
            self._refreshing = False

    def _show_rule(self, index):
        self._updating_rule = True
        try:
            selected = 0 <= index < len(self._rules)
            self.feedback.select_rule(self._rules[index]['phenotype_id'] if selected else None)
            self.detail.setVisible(selected)
            if not selected:
                return
            row = self._rules[index]
            self._expression(row)
        finally:
            self._updating_rule = False

    def _expression(self, row):
        self.expression.set_rule(row)
        self.expression.setVisible(any(state != 'not_used' for state in row['marker_states'].values()))
        self.add_condition.setVisible(any(state == 'not_used' for state in row['marker_states'].values()))

    def _change_condition(self, marker, state):
        index = self.rules.currentRow()
        if index < 0 or marker not in self.markers or state not in {'positive', 'negative', 'not_used'}:
            return
        row = self._rules[index]
        if row['marker_states'][marker] == state:
            return
        row['marker_states'][marker] = state
        self._expression(row)
        self.changed.emit()

    def _name(self, name, *, replacing=None):
        name = name.strip()
        if name.casefold() == UNMATCHED_PHENOTYPE.casefold():
            raise ValueError(f"{UNMATCHED_PHENOTYPE} is reserved for cells that match no phenotype rule.")
        if not name or any(row['phenotype_id'].casefold() == name.casefold()
                           for i, row in enumerate(self._rules) if i != replacing):
            raise ValueError('Enter a distinct phenotype name.')
        return name

    def _add_inline(self):
        number = len(self._rules) + 1
        names = {row['phenotype_id'].casefold() for row in self._rules}
        while f'Phenotype {number}'.casefold() in names:
            number += 1
        self.add_rule(f'Phenotype {number}')
        editor = self.rules.itemWidget(self.rules.currentItem()).name
        editor.setFocus()
        editor.selectAll()

    def _rows_moved(self, *_):
        order = [self.rules.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.rules.count())]
        self._rules = [self._rules[i] for i in order]
        selected = self.rules.currentRow()
        QTimer.singleShot(0, lambda: self._refresh(selected))

    def add_rule(self, name):
        self._rules.append({'phenotype_id': self._name(name),
                            'marker_states': dict.fromkeys(self.markers, 'not_used')})
        self._refresh(len(self._rules) - 1)

    def rename_rule(self, name):
        index = self.rules.currentRow()
        self._rules[index]['phenotype_id'] = self._name(name, replacing=index)
        self._refresh(index)

    def remove_rule(self):
        index = self.rules.currentRow()
        if index >= 0:
            self._rules.pop(index)
            self._refresh(min(index, len(self._rules) - 1))

    def move_rule(self, offset):
        self.move_rule_to(self.rules.currentRow(), self.rules.currentRow() + offset)

    def move_rule_to(self, index, target):
        if 0 <= index < len(self._rules) and 0 <= target < len(self._rules):
            self._rules.insert(target, self._rules.pop(index))
            self._refresh(target)
