"""Supplement the verified IM3 acquisition layout at initial inspection only.

IM3 stores binary records, not vendor XML. Selected original record payloads and
source locations are retained in OME OriginalMetadata. Unknown layouts leave the
Bio-Formats metadata untouched; this module never decodes or changes pixels.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import json
import logging
import math
from pathlib import Path
import struct

from ome_types import OME, from_xml
from ome_types.model import (
    AnnotationRef, Detector, DetectorSettings, Instrument, InstrumentRef,
    Plane, StructuredAnnotations,
)
from xml.etree import ElementTree as ET

_LOG = logging.getLogger(__name__)
_KEY = "IM3 selected binary acquisition records v1"
_NAMESPACE = "openmicroscopy.org/OriginalMetadata"
_MAX_METADATA = 4 * 1024 * 1024


class _UnsupportedIM3(ValueError):
    """The source cannot support an unambiguous metadata supplement."""


@dataclass(frozen=True)
class _Record:
    name: str
    kind: int
    offset: int
    size: int


class _Records:
    """Bounded, seek-only access to the selected binary record containers."""

    def __init__(self, stream, size):
        self.stream, self.size = stream, size
        self.budget = _MAX_METADATA
        self.cache = {}
        self.count = 0

    def read(self, offset, size):
        if offset < 0 or size < 0 or offset + size > self.size or size > self.budget:
            raise _UnsupportedIM3("metadata byte range or budget exceeded")
        self.budget -= size
        self.stream.seek(offset)
        value = self.stream.read(size)
        if len(value) != size:
            raise _UnsupportedIM3("truncated metadata")
        return value

    def records(self, start, end):
        key = (start, end)
        if key in self.cache:
            return self.cache[key]
        if not 0 <= start <= end <= self.size:
            raise _UnsupportedIM3("invalid container bounds")
        result = []
        pos = start
        while end - pos > 8:
            self.count += 1
            if self.count > 10000:
                raise _UnsupportedIM3("metadata record limit exceeded")
            length = struct.unpack('<i', self.read(pos, 4))[0]
            if not 0 <= length <= 1024 or pos + 12 + length > end:
                raise _UnsupportedIM3("invalid record name length")
            name = self.read(pos + 4, length).decode('utf-8')
            size, kind = struct.unpack('<ii', self.read(pos + 4 + length, 8))
            offset = pos + 12 + length
            size -= 8
            if size < 0 or offset + size > end:
                raise _UnsupportedIM3("record exceeds containing bounds")
            result.append(_Record(name, kind, offset, size))
            pos = offset + size
        if any(self.read(pos, end - pos)):
            raise _UnsupportedIM3("nonzero container trailer")
        self.cache[key] = result
        return result

    def children(self, record):
        if record.kind != 0 or record.size < 8:
            raise _UnsupportedIM3("expected supported container")
        return self.records(record.offset + 8, record.offset + record.size)

    def nested(self, record):
        return self.children(_one(self.children(record), ''))

    def numeric(self, record, kind, count=1):
        if record.kind != kind or record.size > 16400:
            raise _UnsupportedIM3("unsupported numeric record")
        data = self.read(record.offset, record.size)
        if len(data) < 12:
            raise _UnsupportedIM3("short numeric record")
        code = struct.unpack_from('<i', data)[0]
        if code == 0:
            actual, begin = 1, 4
        elif code == 1:
            actual, begin = struct.unpack_from('<i', data, 4)[0], 8
            # Native camera properties use count zero for one indexed value.
            if actual == 0 and len(data) == 16 and kind in (6, 7):
                actual = 1
        else:
            raise _UnsupportedIM3("unsupported numeric encoding")
        width, fmt = (8, 'd') if kind == 8 else (4, 'i' if kind == 6 else 'f')
        if actual != count or len(data) != begin + count * width + 4 or any(data[-4:]):
            raise _UnsupportedIM3("numeric cardinality or trailer mismatch")
        values = struct.unpack_from('<' + str(count) + fmt, data, begin)
        if not all(math.isfinite(v) for v in values):
            raise _UnsupportedIM3("nonfinite imaging setting")
        return values

    def string(self, record):
        if record.kind != 10 or not 12 <= record.size <= 16384:
            raise _UnsupportedIM3("unsupported scalar string")
        data = self.read(record.offset, record.size)
        code, length = struct.unpack_from('<ii', data)
        if code != 0 or length < 0 or len(data) != length + 12 or any(data[-4:]):
            raise _UnsupportedIM3("unsupported string encoding")
        return data[8:8 + length].decode('utf-8')

    def retained(self, record):
        data = self.read(record.offset, record.size)
        return {'name': record.name, 'native_record_type': record.kind,
                'payload_offset': record.offset, 'payload_size': record.size,
                'payload_sha256': hashlib.sha256(data).hexdigest(),
                'payload_base64': base64.b64encode(data).decode('ascii')}


def _one(records, name):
    found = [r for r in records if r.name == name]
    if len(found) != 1:
        raise _UnsupportedIM3(f"expected exactly one {name!r} record")
    return found[0]


def _acquisition(path, pixels):
    with path.open('rb') as stream:
        reader = _Records(stream, path.stat().st_size)
        if reader.read(0, 4) != struct.pack('<i', 1985):
            raise _UnsupportedIM3("unsupported IM3 cookie")
        top = reader.records(4, reader.size)
        if reader.numeric(_one(top, 'FileVersion'), 6) != (0,):
            raise _UnsupportedIM3('unsupported IM3 file version')
        root = reader.children(_one(top, ''))
        datasets = reader.children(_one(root, 'DataSet'))
        dataset = reader.children(_one(datasets, ''))
        shape = reader.numeric(_one(dataset, 'Shape'), 6, 3)
        if shape != (pixels.size_x, pixels.size_y, pixels.size_c) or not 1 <= shape[2] <= 4096:
            raise _UnsupportedIM3("native shape differs from Bio-Formats")
        raw = _one(dataset, 'Data')
        if raw.kind != 3 or raw.size < 20:
            # Observed native image records and their payload header use type 3.
            raise _UnsupportedIM3("unsupported native pixel record")
        header = struct.unpack('<4i', reader.read(raw.offset, 16))
        if header != (3, *shape) or raw.size != 20 + math.prod(shape) * 2:
            raise _UnsupportedIM3("native pixel header differs from scalar UInt16 layout")
        if any(reader.read(raw.offset + raw.size - 4, 4)):
            raise _UnsupportedIM3('nonzero native pixel trailer')
        spectra = _one(dataset, 'Spectra')
        rows = reader.children(spectra)
        if len(rows) != shape[2] or any(r.name for r in rows):
            raise _UnsupportedIM3("spectral count differs from image channels")
        bands = []
        for row in rows:
            fields = reader.children(row)
            spectrum = reader.nested(_one(fields, 'Spectrum'))
            wavelength = reader.numeric(_one(spectrum, 'Wavelengths'), 7)[0]
            if wavelength <= 0 or reader.numeric(_one(spectrum, 'Magnitudes'), 7) != (1.0,):
                raise _UnsupportedIM3("not a sampled measurement wavelength")
            settings = reader.nested(_one(fields, 'AcquisitionSettings'))
            excitation = reader.string(_one(settings, 'ExcitationFilterName'))
            emission = reader.string(_one(settings, 'EmissionFilterName'))
            if not excitation or not emission:
                raise _UnsupportedIM3("unidentified imaging filter")
            camera = reader.nested(_one(settings, 'CameraState'))
            parameters = reader.nested(_one(camera, 'Parameters'))
            values = {}
            for item in reader.children(_one(parameters, 'Parameters')):
                pair = reader.children(item)
                name = reader.string(_one(pair, 'Name'))
                if name in values:
                    raise _UnsupportedIM3("duplicate camera parameter")
                if name in {'Exposure', 'ResolutionBits', 'GainFactor', 'Offset'}:
                    values[name] = reader.numeric(_one(pair, 'Value'), 6)[0]
            if set(values) != {'Exposure', 'ResolutionBits', 'GainFactor', 'Offset'}:
                raise _UnsupportedIM3("missing supported camera parameters")
            binning = reader.nested(_one(camera, 'Binning'))
            x, y = (reader.numeric(_one(binning, key), 6)[0] for key in ('X', 'Y'))
            if x != y or x not in (1, 2, 4, 8) or values['GainFactor'] <= 0 or values['Exposure'] <= 0:
                raise _UnsupportedIM3("unsupported gain, exposure or binning")
            if not 1 <= values['ResolutionBits'] <= 16:
                raise _UnsupportedIM3("invalid camera bit depth")
            matches = []
            for item in reader.children(_one(settings, 'WavelengthsAndExposures')):
                pair = reader.children(item)
                if reader.numeric(_one(pair, 'Wavelength'), 7)[0] == wavelength:
                    matches.append(reader.numeric(_one(pair, 'ExposureTime'), 6)[0])
            if matches != [values['Exposure']]:
                raise _UnsupportedIM3("ambiguous per-wavelength exposure")
            bands.append({'excitation_filter': excitation, 'emission_filter': emission,
                          'wavelength_nm': wavelength, 'exposure_microseconds': values['Exposure'],
                          'gain_factor': values['GainFactor'], 'binning_side': x,
                          'resolution_bits': values['ResolutionBits'],
                          'camera_offset_setting_not_subtracted': values['Offset']})
        names = [f"{b['excitation_filter']}:{b['wavelength_nm']:g}nm" for b in bands]
        if len(set(names)) != len(names) or len({b['resolution_bits'] for b in bands}) != 1:
            raise _UnsupportedIM3("ambiguous measurement identity or mixed detector bit depth")
        scale = reader.numeric(_one(dataset, 'MillimetersPerPixel'), 7)[0]
        if scale <= 0:
            raise _UnsupportedIM3("invalid physical pixel size")
        # Keep these bytes exactly, including unknown nested fields; no interpretation
        # of camera offsets, dark-current mean, response or processing state occurs.
        retained = [reader.retained(_one(dataset, name)) for name in
                    ('Shape', 'Spectra', 'Protocol', 'MillimetersPerPixel', 'ReferenceCalibrationTable', 'PixelUnit')]
        return bands, names, scale * 1000, retained


def supplement_im3_metadata(path: Path, metadata: OME, reader_name: str) -> OME:
    """Return a source-derived OME copy for the supported initial IM3 inspection.

    Unsupported/truncated metadata is a recoverable absence, not authorization to
    invent calibration. Never call this from retained-OME pixel reopening.
    """
    if reader_name != 'loci.formats.in.IM3Reader':
        return metadata
    try:
        if len(metadata.images) != 1:
            raise _UnsupportedIM3("only a single image dataset is supported")
        pixels = metadata.images[0].pixels
        if (pixels.size_z, pixels.size_t, pixels.type.value) != (1, 1, 'uint16'):
            raise _UnsupportedIM3("unsupported source topology or pixel type")
        if len(pixels.channels) != pixels.size_c:
            raise _UnsupportedIM3('incomplete reader channel inventory')
        if metadata.images[0].instrument_ref is not None or metadata.instruments:
            raise _UnsupportedIM3('reader already supplies instrument metadata; review required')
        bands, names, scale, retained = _acquisition(Path(path), pixels)
        # Native Camera ResolutionBits describes acquisition hardware. Actual
        # stored pixels can exceed that ADC range, so it stays provenance-only.
        # Do not replace Bio-Formats' stored SignificantBits with camera depth.
        if pixels.planes or any(c.name or c.detector_settings or c.emission_wavelength for c in pixels.channels):
            raise _UnsupportedIM3("reader already supplies imaging settings; review required")
        if pixels.physical_size_x is not None or pixels.physical_size_y is not None:
            raise _UnsupportedIM3("reader already supplies physical calibration")
        result = metadata.model_copy(deep=True)
        image = result.images[0]
        px = image.pixels
        instrument_id, detector_id = 'Instrument:IM3Acquisition:0', 'Detector:IM3Acquisition:0'
        result.instruments.append(Instrument(id=instrument_id, detectors=[Detector(id=detector_id)]))
        image.instrument_ref = InstrumentRef(id=instrument_id)
        px.physical_size_x = px.physical_size_y = scale
        px.physical_size_x_unit = px.physical_size_y_unit = 'µm'
        for index, (band, name, channel) in enumerate(zip(bands, names, px.channels)):
            channel.name = name
            channel.emission_wavelength = band['wavelength_nm']
            channel.emission_wavelength_unit = 'nm'
            channel.detector_settings = DetectorSettings(id=detector_id, gain=band['gain_factor'], binning=f"{band['binning_side']}x{band['binning_side']}")
            px.planes.append(Plane(the_z=0, the_t=0, the_c=index,
                                   exposure_time=band['exposure_microseconds'] / 1000000,
                                   exposure_time_unit='s'))
        proof = {'schema': 'mif-im3-binary-acquisition/v1', 'reader': reader_name,
                 'source_filename': Path(path).name, 'source_size_bytes': Path(path).stat().st_size,
                 'source_identity_binding': 'enclosing inspected-source authority SHA256',
                 'original_format': 'typed binary records; not original XML',
                 'original_bioformats_significant_bits': pixels.significant_bits,
                 'camera_depth_policy': 'provenance only; not stored meaningful bits',
                 'stored_intensity_scale': 'unresolved; camera depth and PixelUnit do not establish stored processing history',
                 'exposure_unit_interpretation': 'native microseconds; protocol US fields and vendor ExposureTime convention',
                 'offset_and_response_application': 'none', 'bands': bands,
                 'selected_native_records': retained}
        # Use ome-types' public XML parser for its own wildcard element model.
        # xsdata's generic AnyElement is not serializable by all ome-types releases.
        wrapper = ET.Element('OME', xmlns='http://www.openmicroscopy.org/Schemas/OME/2016-06')
        annotations = ET.SubElement(wrapper, 'StructuredAnnotations')
        annotation_xml = ET.SubElement(annotations, 'XMLAnnotation',
            ID='Annotation:IM3BinaryAcquisition:0', Namespace=_NAMESPACE)
        value = ET.SubElement(annotation_xml, 'Value')
        original = ET.SubElement(value, 'OriginalMetadata', xmlns='')
        ET.SubElement(original, 'Key').text = _KEY
        ET.SubElement(original, 'Value').text = json.dumps(proof, separators=(',', ':'), allow_nan=False)
        annotation = from_xml(ET.tostring(wrapper, encoding='unicode')).structured_annotations.xml_annotations[0]
        if result.structured_annotations is None:
            result.structured_annotations = StructuredAnnotations()
        result.structured_annotations.xml_annotations.append(annotation)
        image.annotation_refs.append(AnnotationRef(id=annotation.id))
        return result
    except (_UnsupportedIM3, OSError, UnicodeError, struct.error, ValueError) as error:
        _LOG.warning('IM3 image metadata unavailable for %s: %s; retaining Bio-Formats fields for review', path, error)
        return metadata
