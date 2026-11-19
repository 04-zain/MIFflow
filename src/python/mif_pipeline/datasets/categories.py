"""Field-scoped Int32 vocabularies retained with each completed result.

The application codes describe biological labels; Parquet's internal page
dictionaries independently compress those stored primitive values.
"""

from dataclasses import dataclass
import pyarrow as pa
import pyarrow.compute as pc


@dataclass(frozen=True)
class CategoryDictionary:
    """One immutable field/result vocabulary with explicit code meanings."""

    field: str
    dictionary_id: str
    labels: tuple[str, ...]
    label_ids: tuple[str, ...] = ()
    colors: tuple[object, ...] = ()

    @classmethod
    def from_labels(cls, field, dictionary_id, labels, *, zero_label=None):
        """Freeze label order, optionally reserving zero for Unclassified.

        Null is represented by Arrow validity and occupies no dictionary entry.
        Label identity and display name initially agree; their association is
        retained in the result description rather than inferred from part order.
        """
        ordered = tuple(dict.fromkeys(labels))
        if zero_label is not None:
            ordered = (zero_label, *(label for label in ordered if label != zero_label))
        if not field or not dictionary_id or any(not isinstance(label, str) for label in ordered):
            raise ValueError("Category dictionaries require a field, result identity and string labels")
        if len(ordered) > 2**31:
            raise ValueError("The category vocabulary exceeds signed Int32 codes")
        return cls(field, dictionary_id, ordered)

    def encode(self, labels):
        """Return primitive Int32 codes and preserve unavailable values as null."""
        values = pa.array(labels, type=pa.string())
        codes = pc.index_in(values, value_set=pa.array(self.labels, type=pa.string()))
        unknown = pc.and_(pc.is_valid(values), pc.is_null(codes))
        if pc.any(unknown).as_py():
            raise ValueError(f"Unknown label in category field {self.field}")
        return codes.cast(pa.int32())

    def decode(self, codes):
        """Resolve names at an export boundary using this field's frozen mapping."""
        values = pa.array(codes, type=pa.int32())
        invalid = pc.or_(pc.less(values, 0), pc.greater_equal(values, len(self.labels)))
        if pc.any(invalid).as_py():
            raise ValueError(f"Unknown category code in field {self.field}")
        return pc.take(pa.array(self.labels, type=pa.string()), values).to_pylist()

    def to_mapping(self):
        return {
            "dictionary_id": self.dictionary_id,
            "field": self.field,
            "version": 1,
            "entries": [
                {"code": code, "label_id": self.label_ids[code] if self.label_ids else label, "name": label, "color": self.colors[code] if self.colors else None}
                for code, label in enumerate(self.labels)
            ],
        }

    @classmethod
    def from_mapping(cls, value):
        entries = value["entries"]
        if value.get("version") != 1 or [entry["code"] for entry in entries] != list(range(len(entries))):
            raise ValueError("Category dictionary codes or version are invalid")
        labels = tuple(entry["name"] for entry in entries)
        label_ids = tuple(entry["label_id"] for entry in entries)
        if any(not isinstance(item, str) for item in (*labels, *label_ids)) or len(set(label_ids)) != len(label_ids):
            raise ValueError("Category identities and names must be declared strings")
        return cls(value["field"], value["dictionary_id"], labels, label_ids, tuple(entry["color"] for entry in entries))
