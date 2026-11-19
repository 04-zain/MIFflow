"""Resumable pre-tissue rigid registration over inspected original cycle files.

This draft owns choices and reviewed transformations only. It creates neither a
paired-image project nor a published logical sample; the caller groups sources
after the final common crop has been accepted.
"""
from __future__ import annotations
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from tempfile import mkdtemp

from mif_pipeline.io.cycle_registration import (
    AcceptedCycleRegistration, CycleGrid, CycleRigidTransform, common_crop, validate_crop)
from mif_pipeline.qupath_runtime.registration_editor import RegistrationEditorSession


def _write(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, sort_keys=True, allow_nan=False), encoding='utf-8')
    temporary.replace(path)


class CycleEditorSession(RegistrationEditorSession):
    """Keep the last applied position when the researcher leaves a cycle."""
    def request(self, operation, **values):
        self.owner.validate_sources()
        result = super().request(operation, **values)
        self.owner.validate_sources()
        if operation in {'pose', 'affine', 'save'}:
            transform = CycleRigidTransform(tuple(result['matrix']))
            self.owner.transforms[self.member_index] = transform
            self.owner.reviewed[self.member_index] = operation == 'save'
            if operation != 'save':
                self.owner.crop = None
            if operation == 'save':
                self.owner.evidence[self.member_index] = result['evidence']
            elif operation == 'affine':
                self.owner.evidence[self.member_index] = dict(method=result['estimate']['method'],
                    estimate=result['estimate'], settings=values['settings'])
            else:
                self.owner.evidence[self.member_index] = dict(method='manual-rigid', estimate={}, settings={})
            self.owner.evidence[self.member_index]['native_directory'] = str(self.root)
            self.owner.save()
        return result


@dataclass
class CycleRegistrationSession:
    collection: object
    root: Path
    grids: tuple[CycleGrid, ...]
    channel_names: tuple[tuple[str, ...], ...]
    reference_member: int
    registration_channels: tuple[int, ...]
    shared_stain: str
    transforms: list[CycleRigidTransform]
    reviewed: list[bool]
    evidence: list[dict]
    crop: tuple[int, int, int, int] | None = None

    @classmethod
    def open(cls, collection, workspace):
        from mif_pipeline.io import open_image
        from mif_pipeline.io.canonical_ingestion import resolve_canonical_image_selection
        from mif_pipeline.io.canonical_ome_tiff import _physical_calibration
        from mif_pipeline.io.backends import snapshot_opened_source_identity
        key = hashlib.sha256(json.dumps(collection.to_mapping(), sort_keys=True,
            separators=(',', ':'), allow_nan=False).encode()).hexdigest()
        root = Path(workspace).resolve() / key
        root.mkdir(parents=True, exist_ok=True)
        grids, names = [], []
        for member in collection.members:
            snapshot_opened_source_identity(member.path, retained=member.identity)
            with open_image(member.path) as source:
                if source.identity != member.identity:
                    raise ValueError('A cycle source changed after inspection')
                selection = resolve_canonical_image_selection(source.metadata, source.series,
                    image_index=member.image_index, series_index=member.series_index,
                    require_planar_channels=True)
                pixels = source.metadata.images[selection.source_image_index].pixels
                if pixels.size_c != member.channel_count:
                    raise ValueError('A cycle channel inventory changed after inspection')
                x = _physical_calibration(pixels.physical_size_x, pixels.physical_size_x_unit, 'X')[2]
                y = _physical_calibration(pixels.physical_size_y, pixels.physical_size_y_unit, 'Y')[2]
                grids.append(CycleGrid(pixels.size_x, pixels.size_y, x, y))
                names.append(tuple(c.name or f'Channel {i+1}' for i, c in enumerate(pixels.channels)))
        count = len(grids)
        session = cls(collection, root, tuple(grids), tuple(names), 0, (-1,)*count, '',
            [CycleRigidTransform.identity() for _ in grids], [False]*count, [{} for _ in grids])
        saved = root / 'session.json'
        if saved.exists():
            if saved.is_symlink() or saved.stat().st_size > 1024*1024:
                raise ValueError('The cycle registration draft is invalid')
            value = json.loads(saved.read_text())
            if (value.get('schema') != 'cycle-registration-draft/v1'
                    or value.get('collection') != collection.to_mapping()
                    or value.get('grids') != [g.to_mapping() for g in grids]):
                raise ValueError('The saved registration belongs to different cycle sources')
            session.configure(value['reference_member'], tuple(value['registration_channels']),
                              value['shared_stain'], save=False)
            transforms = [CycleRigidTransform(tuple(v)) for v in value['transforms']]
            if (len(transforms) != count or len(value['reviewed']) != count
                    or any(type(v) is not bool for v in value['reviewed'])
                    or len(value['evidence']) != count
                    or transforms[session.reference_member] != CycleRigidTransform.identity()):
                raise ValueError('The saved cycle transformations are invalid')
            session.transforms, session.reviewed, session.evidence = transforms, value['reviewed'], value['evidence']
            if value.get('crop') is not None:
                session.crop = validate_crop(session.grids, transforms, session.reference_member, tuple(value['crop']))
        return session

    def validate_sources(self):
        from mif_pipeline.io.canonical_ingestion import _validate_inspected_source_stat
        for member in self.collection.members:
            _validate_inspected_source_stat(member.identity)

    def configure(self, reference, channels, stain, *, save=True):
        if type(reference) is not int or not 0 <= reference < len(self.grids):
            raise ValueError('Select one fixed reference cycle')
        if (type(channels) is not tuple or len(channels) != len(self.grids)
                or any(type(c) is not int or not 0 <= c < m.channel_count
                       for c, m in zip(channels, self.collection.members))):
            raise ValueError('Select the corresponding shared-stain channel in every cycle')
        if not isinstance(stain, str) or not stain.strip() or len(stain.strip()) > 200:
            raise ValueError('Name the actual shared stain used in every cycle')
        if (reference, channels, stain.strip()) != (self.reference_member, self.registration_channels, self.shared_stain):
            self.transforms = [CycleRigidTransform.identity() for _ in self.grids]
            self.reviewed = [i == reference for i in range(len(self.grids))]
            self.evidence = [{} for _ in self.grids]
            self.crop = None
        self.reference_member, self.registration_channels, self.shared_stain = reference, channels, stain.strip()
        if save: self.save()

    def save(self):
        self.validate_sources()
        _write(self.root / 'session.json', dict(schema='cycle-registration-draft/v1',
            collection=self.collection.to_mapping(), grids=[g.to_mapping() for g in self.grids],
            reference_member=self.reference_member, registration_channels=list(self.registration_channels),
            shared_stain=self.shared_stain, transforms=[list(t.matrix) for t in self.transforms],
            reviewed=self.reviewed, evidence=self.evidence, crop=self.crop))

    def record(self, member_index, transform, evidence):
        if member_index == self.reference_member or not 0 <= member_index < len(self.grids):
            raise ValueError('Select a moving cycle for registration')
        if type(transform) is not CycleRigidTransform or type(evidence) is not dict:
            raise ValueError('A checked rigid transform and fitting record are required')
        self.transforms[member_index] = transform
        self.crop = None
        self.reviewed[member_index] = True
        self.evidence[member_index] = evidence
        self.save()

    def accept(self, crop=None):
        self.validate_sources()
        if not all(self.reviewed):
            raise ValueError('Review and use the alignment for every moving cycle first')
        if crop is None:
            crop = self.crop or common_crop(self.grids, self.transforms, self.reference_member)
        validate_crop(self.grids, self.transforms, self.reference_member, crop)
        return AcceptedCycleRegistration(self.reference_member, self.registration_channels,
            self.shared_stain, self.grids, tuple(self.transforms), crop)

    def create_editor(self, member_index, *, tools_directory=None):
        """Adapt raw source readers to the existing native editor protocol."""
        from mif_pipeline._defaults import default_section
        from mif_pipeline.qupath_runtime.scripts import QuPathScriptAsset, materialize_evidence_script
        from .automatic import find_elastix_directory
        self.validate_sources()
        if member_index == self.reference_member or not self.shared_stain:
            raise ValueError('Choose a shared stain and a moving cycle first')
        root = Path(mkdtemp(prefix=f'cycle-{member_index+1}-', dir=self.root))
        assets = (QuPathScriptAsset.REGISTRATION_EDITOR, QuPathScriptAsset.AUTOMATIC_ELASTIX,
                  QuPathScriptAsset.PAIRED_GEOMETRY)
        scripts = {Path(asset.value).name: materialize_evidence_script(root, asset) for asset in assets}
        helpers = {f'registration/{name}': dict(path=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest())
                   for name, path in scripts.items() if name != 'registration_editor_worker.groovy'}
        try:
            tools = str(find_elastix_directory(tools_directory))
        except (OSError, ValueError):
            tools = None
        defaults = dict(registration=default_section('registration.automatic'),
            bspline=default_section('registration.bspline'), manual_registration=default_section('registration.manual'))
        defaults['registration'].update(fixed_channel=self.registration_channels[self.reference_member],
            moving_channel=self.registration_channels[member_index], orientation_search=False)
        request = dict(schema='cycle-registration-session/v1', exchange_directory=str(root), helpers=helpers,
            entries={role: dict(member=self.collection.members[index].to_mapping(), grid=self.grids[index].to_mapping())
                     for role, index in (('fixed', self.reference_member), ('moving', member_index))},
            initial_transform=list(self.transforms[member_index].matrix), editor_defaults=defaults,
            initial_evidence=self.evidence[member_index],
            reference_display=dict(mode='alignment', method='manual', tools_directory=tools),
            shared_stain=self.shared_stain)
        path = root / 'setup.json'
        _write(path, request)
        editor = CycleEditorSession(root, None, scripts['registration_editor_worker.groovy'], path,
            request['reference_display'], self.root / 'appearance.json')
        editor.owner, editor.member_index = self, member_index
        return editor
