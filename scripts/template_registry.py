#!/usr/bin/env python3
"""Register an intact local PPTX and resolve the project default. Python 3 only."""
import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import posixpath
import re
import shutil
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile

ROOT = Path(__file__).resolve().parents[1]
NS = {
    'p': 'http://schemas.openxmlformats.org/presentationml/2006/main',
    'a': 'http://schemas.openxmlformats.org/drawingml/2006/main',
    'r': 'http://schemas.openxmlformats.org/officeDocument/2006/relationships',
}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent,
                                     delete=False) as handle:
        temporary = Path(handle.name)
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write('\n')
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def inspect_pptx(path):
    with zipfile.ZipFile(path) as archive:
        broken = archive.testzip()
        if broken:
            raise ValueError(f'Corrupt PPTX entry: {broken}')

        def xml(part):
            return ET.fromstring(archive.read(part))

        def relations(part):
            folder, name = posixpath.split(part)
            relpart = posixpath.join(folder, '_rels', name + '.rels')
            if relpart not in archive.namelist():
                return {}
            result = {}
            for rel in xml(relpart):
                if rel.get('TargetMode') == 'External':
                    continue
                target = rel.attrib['Target']
                target = target.lstrip('/') if target.startswith('/') else posixpath.normpath(posixpath.join(folder, target))
                result[rel.attrib['Id']] = {
                    'part': target, 'kind': rel.attrib['Type'].rsplit('/', 1)[-1]}
            return result

        def target(part, kind):
            return next((r['part'] for r in relations(part).values()
                         if r['kind'] == kind), None)

        def placeholders(tree):
            result = []
            for shape in tree.findall('.//p:sp', NS):
                ph = shape.find('p:nvSpPr/p:nvPr/p:ph', NS)
                if ph is None:
                    continue
                props = shape.find('p:nvSpPr/p:cNvPr', NS)
                result.append({
                    'shape_id': props.get('id') if props is not None else None,
                    'name': props.get('name') if props is not None else None,
                    'type': ph.get('type', 'obj'), 'index': ph.get('idx', '0')})
            return result

        presentation = xml('ppt/presentation.xml')
        size = presentation.find('p:sldSz', NS)
        if size is None:
            raise ValueError('PPTX has no slide dimensions')
        slide_rels = relations('ppt/presentation.xml')
        slides = []
        for index, sid in enumerate(presentation.findall('p:sldIdLst/p:sldId', NS), 1):
            part = slide_rels[sid.attrib['{' + NS['r'] + '}id']]['part']
            layout = target(part, 'slideLayout')
            slides.append({'number': index, 'part': part, 'layout': layout,
                           'master': target(layout, 'slideMaster') if layout else None})
        master_usage = Counter(s['master'] for s in slides if s['master'])
        layouts, masters, themes = [], [], []
        fonts, sizes, colors = Counter(), Counter(), Counter()
        for part in sorted(archive.namelist()):
            if not part.endswith('.xml') or '/_rels/' in part:
                continue
            if part.startswith('ppt/slideLayouts/'):
                tree = xml(part)
                common = tree.find('p:cSld', NS)
                layouts.append({'part': part, 'name': common.get('name', '') if common is not None else '',
                                'master': target(part, 'slideMaster'),
                                'placeholders': placeholders(tree)})
            elif part.startswith('ppt/slideMasters/'):
                masters.append({'part': part, 'theme': target(part, 'theme'),
                                'example_slide_count': master_usage[part]})
            elif part.startswith('ppt/theme/'):
                tree = xml(part)
                scheme = tree.find('.//a:clrScheme', NS)
                palette = {}
                if scheme is not None:
                    for item in scheme:
                        if len(item):
                            color = item[0]
                            palette[item.tag.rsplit('}', 1)[-1]] = dict(color.attrib)
                themes.append({'part': part, 'name': tree.get('name'), 'colors': palette})
            if part.startswith(('ppt/slides/', 'ppt/slideLayouts/', 'ppt/slideMasters/')):
                tree = xml(part)
                for node in tree.iter():
                    if node.tag in ('{' + NS['a'] + '}latin', '{' + NS['a'] + '}ea'):
                        if node.get('typeface'):
                            fonts[node.get('typeface')] += 1
                    if node.tag in ('{' + NS['a'] + '}rPr', '{' + NS['a'] + '}defRPr', '{' + NS['a'] + '}endParaRPr') and node.get('sz'):
                        sizes[str(int(node.get('sz')) / 100)] += 1
                    if node.tag == '{' + NS['a'] + '}srgbClr':
                        colors[node.get('val')] += 1
        return {'slide_size_emu': {k: int(size.get(k)) for k in ('cx', 'cy')},
                'slide_count': len(slides), 'master_count': len(masters),
                'layout_count': len(layouts), 'slides': slides,
                'masters': masters, 'layouts': layouts, 'themes': themes,
                'observed_explicit_fonts': dict(fonts.most_common()),
                'observed_explicit_sizes_pt': dict(sizes.most_common()),
                'observed_explicit_colors': dict(colors.most_common()),
                'limits': 'Structural inventory only. Observed values are not approved brand rules. Effective inherited styles, logo identity, rendering, font availability and editing behavior have not been validated.'}


def register(store, source, template_id, name):
    if not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', template_id):
        raise ValueError('Template ID must contain lowercase letters, digits and hyphens')
    source = source.expanduser().resolve(strict=True)
    inventory = inspect_pptx(source)
    sha = digest(source)
    pack = store / template_id
    destination = pack / 'source.pptx'
    if pack.exists():
        if not destination.is_file() or digest(destination) != sha:
            raise ValueError('Template ID already exists with different or incomplete content; use a new version ID')
    else:
        store.mkdir(parents=True, exist_ok=True)
        staged = Path(tempfile.mkdtemp(prefix='.register-', dir=store))
        try:
            shutil.copyfile(source, staged / 'source.pptx')
            if digest(staged / 'source.pptx') != sha:
                raise ValueError('Source changed while copying; retry registration')
            write_json(staged / 'inventory.json', inventory)
            write_json(staged / 'manifest.json', {
                'schema_version': 1, 'id': template_id, 'name': name,
                'source_file': 'source.pptx', 'original_filename': source.name,
                'sha256': sha, 'inventory_file': 'inventory.json',
                'policy': {'preserve_original_package': True,
                           'preserve_all_masters_and_layouts': True,
                           'explicit_template_overrides_default': True},
                'validation': {'package_integrity': 'pass', 'rendering': 'not_run',
                               'brand_rule_extraction': 'not_run', 'editing': 'not_run'}})
            staged.rename(pack)
        finally:
            if staged.exists():
                shutil.rmtree(staged)
    write_json(store / 'default.json', {'schema_version': 1, 'default_template_id': template_id})
    return resolve(store)


def resolve(store):
    settings = json.loads((store / 'default.json').read_text(encoding='utf-8'))
    template_id = settings['default_template_id']
    if not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', template_id):
        raise ValueError('Invalid default template ID')
    pack = store / template_id
    manifest = json.loads((pack / 'manifest.json').read_text(encoding='utf-8'))
    if manifest['id'] != template_id or manifest['source_file'] != 'source.pptx':
        raise ValueError('Template manifest identity mismatch')
    source = pack / 'source.pptx'
    if digest(source) != manifest['sha256']:
        raise ValueError('Default template checksum mismatch; restore or register a new version')
    return {'id': template_id, 'name': manifest['name'], 'source': str(source.resolve()),
            'manifest': str((pack / 'manifest.json').resolve()),
            'inventory': str((pack / 'inventory.json').resolve()),
            'sha256': manifest['sha256']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--store', type=Path, default=ROOT / 'local-templates')
    commands = parser.add_subparsers(dest='command', required=True)
    setter = commands.add_parser('set-default')
    setter.add_argument('source', type=Path)
    setter.add_argument('--id', required=True)
    setter.add_argument('--name', required=True)
    commands.add_parser('resolve')
    args = parser.parse_args()
    try:
        store = args.store.expanduser().resolve()
        result = register(store, args.source, args.id, args.name) if args.command == 'set-default' else resolve(store)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, ET.ParseError) as error:
        print(f'Template error: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
