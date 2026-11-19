"""Select accepted phenotype assignments while retaining bounded cell queries."""
import pyarrow as pa
import pyarrow.compute as pc


def phenotype_choices(dataset):
    dictionary = dataset.dictionaries.get('primary_phenotype')
    if dictionary is None or 'primary_phenotype' not in dataset.schema.names:
        return ()
    return tuple(dict.fromkeys(dictionary.labels))


def filter_phenotype(dataset, phenotype):
    if phenotype is None:
        return dataset
    if not isinstance(phenotype, str) or not phenotype.strip() or phenotype not in phenotype_choices(dataset):
        raise ValueError('Choose a phenotype in the current accepted cell results.')
    return _PhenotypeCells(dataset, phenotype)


class _PhenotypeCells:
    def __init__(self, dataset, phenotype):
        self.dataset = dataset
        labels = dataset.dictionaries['primary_phenotype'].labels
        self.codes = tuple(index for index, label in enumerate(labels) if label == phenotype)
        self.category_count = len(labels)
        self.row_count = None

    def __getattr__(self, name):
        return getattr(self.dataset, name)

    def _batches(self, method, columns, *args, **kwargs):
        fields = tuple(dict.fromkeys((*columns, 'primary_phenotype')))
        total = selected = 0
        for batch in getattr(self.dataset, method)(*args, columns=fields, **kwargs):
            total += batch.num_rows
            values = batch.column('primary_phenotype')
            invalid = pc.or_(pc.less(values, 0), pc.greater_equal(values, self.category_count))
            if pc.any(invalid).as_py():
                raise ValueError('Saved phenotype codes differ from the selected dictionary.')
            matched = batch.filter(pc.is_in(values, value_set=pa.array(self.codes, type=values.type)))
            selected += matched.num_rows
            if matched.num_rows:
                yield matched.select(columns)
        if method == 'iter_batches':
            if total != self.dataset.row_count:
                raise ValueError('Saved cells differ from the accepted population.')
            self.row_count = selected

    def iter_batches(self, *, columns):
        yield from self._batches('iter_batches', columns)

    def query(self, bounds, *, columns, predicate='intersects'):
        yield from self._batches('query', columns, bounds, predicate=predicate)

    def lookup_ids(self, ids, *, columns):
        yield from self._batches('lookup_ids', columns, ids)
