"""Export a .kicad_pcb to a boardview: a self-contained HTML viewer (template.html + data, photo underlays) and an
OpenBoardView .brd ("BRDOUT:" variant). Runs under a Python that has KiCad's pcbnew module.

Usage: boardview.py <board.kicad_pcb> [-o OUTDIR] [--no-photo] [--kicad-nets-only]
  Writes OUTDIR/<board>-boardview.html and OUTDIR/<board>.brd. Default OUTDIR: <board dir>/boardview

Board conventions:
  Photos   reference images embedded in the board, Eco1.User = top, Eco2.User = bottom. The bottom is shown mirrored
           (as seen with the board flipped); place the bottom photo in KiCad as seen through the top.
  Nets     KiCad's own pad nets are kept. Pads KiCad leaves unassigned get a net derived from what copper touches what
           (pads <-> track ends <-> vias <-> zone outlines), named after the touching track/zone's net, else NET_<n>.
           Copper on two different KiCad nets is never merged. --kicad-nets-only skips the derivation.
  Outline  Edge.Cuts, unless <board dir>/boardview/outline.json exists ([[x, y], ...] in mm).
  Parts    references ending in ** or ? (REF**, R?) are shown as unidentified. Single-pad TP* parts with a net are
           exported as .brd nails. Fields holding a URL (Source, Source2, ...) are shown as links."""
import argparse, base64, io, json, math, os, re
import pcbnew

MM = 1e6
TOL = 0.25
PHOTO_WIDTH = 1800
_r = lambda v: round(v / MM, 3)
SHAPES = {pcbnew.PAD_SHAPE_CIRCLE: 'circle', pcbnew.PAD_SHAPE_OVAL: 'oval'}
STD_FIELDS = {'Reference', 'Value', 'Footprint', 'Datasheet', 'Description'}
HERE = os.path.dirname(os.path.abspath(__file__))


# ---- board loading -------------------------------------------------------------------------------------------------

def load(pcb, derive=True):
    """Plain data in mm, KiCad orientation (Y down):
      parts    [{ref, val, fp, x, y, rot, side, bbox, fields, pads: [[num, shape, x, y, w, h, angle, net, smd|tht]]}]
      tracks   [[x1, y1, x2, y2, width, net, side]]       vias  [[x, y, dia, net]]
      zones    [{net, sides, poly}]                        outline [[[x, y], ...], ...]
      images   {'F'|'B': {png, x, y, w, h}}"""
    board = pcbnew.LoadBoard(pcb)
    parts = []
    for fp in board.GetFootprints():
        side = 'B' if fp.IsFlipped() else 'F'
        pads = []
        for pad in fp.Pads():
            smd = pad.GetAttribute() == pcbnew.PAD_ATTRIB_SMD
            lay = pcbnew.B_Cu if side == 'B' and smd else pcbnew.F_Cu
            sz, pos = pad.GetSize(lay), pad.GetPosition()
            pads.append([pad.GetNumber(), SHAPES.get(pad.GetShape(lay), 'rect'), _r(pos.x), _r(pos.y), _r(sz.x), _r(sz.y),
                         round(pad.GetOrientationDegrees(), 2), pad.GetNetname() or None, 'smd' if smd else 'tht'])
        # pad-less footprints (outline-only placeholders, e.g. a BGA whose ballout isn't mapped) are kept
        bb, pos = fp.GetBoundingBox(False), fp.GetPosition()
        parts.append({'ref': fp.GetReference(), 'val': fp.GetValue(), 'fp': str(fp.GetFPID().GetUniStringLibItemName()),
                      'x': _r(pos.x), 'y': _r(pos.y), 'rot': round(fp.GetOrientationDegrees(), 2), 'side': side,
                      'bbox': [_r(bb.GetLeft()), _r(bb.GetTop()), _r(bb.GetRight()), _r(bb.GetBottom())],
                      'fields': {f.GetName(): f.GetText() for f in fp.GetFields() if f.GetName() not in STD_FIELDS and f.GetText()},
                      'pads': pads})
    parts.sort(key=lambda p: p['ref'])

    tracks, vias = [], []
    for t in board.GetTracks():
        if t.GetClass() == 'PCB_VIA':
            p = t.GetPosition()
            vias.append([_r(p.x), _r(p.y), _r(t.GetWidth(pcbnew.F_Cu)), t.GetNetname() or None])
        else:  # segments and arcs (arcs as chords)
            s, e = t.GetStart(), t.GetEnd()
            tracks.append([_r(s.x), _r(s.y), _r(e.x), _r(e.y), _r(t.GetWidth()), t.GetNetname() or None,
                           'B' if t.GetLayer() == pcbnew.B_Cu else 'F'])

    zones = []
    for z in board.Zones():
        if z.GetIsRuleArea() or z.Outline().OutlineCount() == 0:
            continue
        ol = z.Outline().Outline(0)   # outline, not the (possibly stale) fill
        sides = ''.join(s for s, l in (('F', pcbnew.F_Cu), ('B', pcbnew.B_Cu)) if z.IsOnLayer(l))
        zones.append({'net': z.GetNetname() or None, 'sides': sides or 'FB',
                      'poly': [[_r(ol.CPoint(i).x), _r(ol.CPoint(i).y)] for i in range(ol.PointCount())]})

    override = os.path.join(os.path.dirname(os.path.abspath(pcb)), 'boardview', 'outline.json')
    if os.path.exists(override):
        outline = [json.load(open(override))]
    else:
        polys = pcbnew.SHAPE_POLY_SET()
        if board.GetBoardPolygonOutlines(polys, True):
            outline = [[[_r(o.CPoint(k).x), _r(o.CPoint(k).y)] for k in range(o.PointCount())]
                       for o in (polys.Outline(i) for i in range(polys.OutlineCount()))]
        else:   # no closed Edge.Cuts: KiCad's fallback box depends on layer visibility (.kicad_prl), so use the parts'
            x0, y0 = min(p['bbox'][0] for p in parts), min(p['bbox'][1] for p in parts)
            x1, y1 = max(p['bbox'][2] for p in parts), max(p['bbox'][3] for p in parts)
            outline = [[[x0, y0], [x1, y0], [x1, y1], [x0, y1]]]

    if derive:
        _derive(parts, tracks, vias, zones)
    return {'title': os.path.splitext(os.path.basename(pcb))[0], 'parts': parts, 'tracks': tracks, 'vias': vias,
            'zones': zones, 'outline': outline, 'images': _images(pcb, board)}


def _images(pcb, board):
    """Reference images: pixel data from the file text, placement from pcbnew, matched up by layer."""
    text = open(pcb, encoding='utf-8').read()
    blobs = {}
    for m in re.finditer(r'\n\t\(image\n.*?\(layer "([^"]+)"\).*?\(data((?:\s*"[^"]*")+)\s*\)', text, re.S):
        blobs.setdefault(m.group(1), ''.join(re.findall(r'"([^"]*)"', m.group(2))))
    out = {}
    for d in board.GetDrawings():
        if d.GetClass() != 'PCB_REFERENCE_IMAGE':
            continue
        name = d.GetLayerName()                       # e.g. "User.Eco1"; the file says "Eco1.User"
        side = 'F' if 'Eco1' in name or name.startswith('F.') else 'B' if 'Eco2' in name or name.startswith('B.') else None
        blob = next((b for l, b in blobs.items() if set(re.split(r'\.', l)) == set(re.split(r'\.', name))), None)
        if side and blob and side not in out:
            bb = d.GetBoundingBox()
            out[side] = {'png': base64.b64decode(blob), 'x': _r(bb.GetLeft()), 'y': _r(bb.GetTop()),
                         'w': _r(bb.GetWidth()), 'h': _r(bb.GetHeight())}
    return out


def _in_pad(x, y, p, tol=TOL):
    dx, dy = x - p[2], y - p[3]
    t = math.radians(p[6])
    u, v = dx*math.cos(t) - dy*math.sin(t), dx*math.sin(t) + dy*math.cos(t)
    return abs(u) <= p[4]/2 + tol and abs(v) <= p[5]/2 + tol


def _seg_dist(x, y, s):
    dx, dy = s[2] - s[0], s[3] - s[1]
    L = dx*dx + dy*dy
    t = 0 if L == 0 else max(0, min(1, ((x - s[0])*dx + (y - s[1])*dy) / L))
    return math.hypot(x - (s[0] + t*dx), y - (s[1] + t*dy))


def _in_poly(x, y, poly):
    c = False
    for i in range(len(poly)):
        (x1, y1), (x2, y2) = poly[i], poly[(i+1) % len(poly)]
        if (y1 > y) != (y2 > y) and x < (x2-x1)*(y-y1)/(y2-y1) + x1:
            c = not c
    return c


def _derive(parts, tracks, vias, zones):
    """Fill in nets for unassigned pads from copper connectivity (union-find). Sets track[5]/via[3]/zone['net'] too."""
    pads = [q for p in parts for q in p['pads']]
    pad_sides = [p['side'] if q[8] == 'smd' else 'FB' for p in parts for q in p['pads']]   # SMD copper is one side only
    N, nt, nv = len(pads), len(tracks), len(vias)
    T, V, Z = (lambda i: N + i), (lambda i: N + nt + i), (lambda i: N + nt + nv + i)
    netof = [q[7] for q in pads] + [t[5] for t in tracks] + [v[3] for v in vias] + [z['net'] for z in zones]
    par = list(range(len(netof)))

    def find(i):
        while par[i] != i:
            par[i] = par[par[i]]
            i = par[i]
        return i

    def union(a, b):   # never join two different KiCad nets
        if netof[a] and netof[b] and netof[a] != netof[b]:
            return
        par[find(a)] = find(b)

    for i, t in enumerate(tracks):
        for x, y in ((t[0], t[1]), (t[2], t[3])):
            for k, q in enumerate(pads):
                if t[6] in pad_sides[k] and _in_pad(x, y, q):
                    union(T(i), k)
            for k, v in enumerate(vias):
                if math.hypot(x - v[0], y - v[1]) <= v[2]/2 + 0.05:
                    union(T(i), V(k))
            for k, o in enumerate(tracks):
                if k != i and _seg_dist(x, y, o) <= (t[4] + o[4])/2 + 0.02:
                    union(T(i), T(k))
    for zi, z in enumerate(zones):
        for k, q in enumerate(pads):
            if set(z['sides']) & set(pad_sides[k]) and _in_poly(q[2], q[3], z['poly']):
                union(Z(zi), k)
        for i, t in enumerate(tracks):
            if t[6] in z['sides'] and (_in_poly(t[0], t[1], z['poly']) or _in_poly(t[2], t[3], z['poly'])):
                union(Z(zi), T(i))
    for i, n in enumerate(netof):   # same KiCad net name = same net
        if n:
            union(i, netof.index(n))

    names, members = {}, {}
    for i, n in enumerate(netof):
        if n:
            names.setdefault(find(i), n)
    for k in range(N):
        members.setdefault(find(k), []).append(k)
    cnt, root_net = 0, {}
    for root, ks in sorted(members.items(), key=lambda kv: min(kv[1])):
        nm = names.get(root)
        if not nm and len(ks) >= 2:
            cnt += 1
            nm = 'NET_%d' % cnt
        root_net[root] = nm
        for k in ks:
            pads[k][7] = nm
    for i, t in enumerate(tracks):
        t[5] = root_net.get(find(T(i)), t[5])
    for i, v in enumerate(vias):
        v[3] = root_net.get(find(V(i)), v[3])
    for zi, z in enumerate(zones):
        z['net'] = root_net.get(find(Z(zi)), z['net'])


# ---- HTML viewer ---------------------------------------------------------------------------------------------------

def to_jpeg(png):
    """Flatten transparency onto white, scale to PHOTO_WIDTH and re-encode as JPEG (Pillow, else wx); PNG if neither."""
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(png))
        bg = Image.new('RGB', img.size, 'white')
        bg.paste(img, mask=img.convert('RGBA').getchannel('A'))
        if bg.width > PHOTO_WIDTH:
            bg = bg.resize((PHOTO_WIDTH, round(bg.height * PHOTO_WIDTH / bg.width)), Image.LANCZOS)
        buf = io.BytesIO()
        bg.save(buf, 'JPEG', quality=82, optimize=True)
        return 'image/jpeg', buf.getvalue()
    except ImportError:
        pass
    try:
        import wx
        _app = wx.App(False)
        img = wx.Image(io.BytesIO(png), wx.BITMAP_TYPE_PNG)
        if img.HasAlpha():
            bg = wx.Image(img.GetWidth(), img.GetHeight(), clear=False)
            bg.SetRGB(wx.Rect(0, 0, img.GetWidth(), img.GetHeight()), 255, 255, 255)
            bg.Paste(img, 0, 0, wx.IMAGE_ALPHA_BLEND_COMPOSE)
            img = bg
        if img.GetWidth() > PHOTO_WIDTH:
            img = img.Scale(PHOTO_WIDTH, round(img.GetHeight() * PHOTO_WIDTH / img.GetWidth()), wx.IMAGE_QUALITY_HIGH)
        img.SetOption(wx.IMAGE_OPTION_QUALITY, '82')
        buf = io.BytesIO()
        if img.SaveFile(buf, wx.BITMAP_TYPE_JPEG):
            return 'image/jpeg', buf.getvalue()
    except Exception as e:
        print('JPEG re-encode unavailable (%s); embedding PNG' % e)
    return 'image/png', png


def build_html(d, photo=True):
    photos = {}
    for side, im in (d['images'].items() if photo else ()):
        mime, data = to_jpeg(im['png'])
        photos[side] = {'src': 'data:%s;base64,%s' % (mime, base64.b64encode(data).decode()),
                        'x': im['x'], 'y': im['y'], 'w': im['w'], 'h': im['h']}
    data = {k: v for k, v in d.items() if k != 'images'}
    data['photos'] = photos
    tpl = open(os.path.join(HERE, 'template.html'), encoding='utf-8').read()
    html = tpl.replace('/*DATA*/', 'const BV=' + json.dumps(data, separators=(',', ':')) + ';')
    parts = d['parts']
    summary = 'parts %d (top %d, bottom %d), pads %d, tracks %d, vias %d, zones %d, nets %d, photos %s, %d KB' % (
        len(parts), sum(p['side'] == 'F' for p in parts), sum(p['side'] == 'B' for p in parts),
        sum(len(p['pads']) for p in parts), len(d['tracks']), len(d['vias']), len(d['zones']),
        len({q[7] for p in parts for q in p['pads'] if q[7]}), ','.join(sorted(photos)) or 'none', len(html) // 1024)
    return html, summary


# ---- OpenBoardView .brd --------------------------------------------------------------------------------------------

def build_brd(d):
    """BRDOUT: variant; units mils, Y up. Per OpenBoardView BRD2File.cpp, a part's pin field is the index of its
    FIRST pin, and bottom-side parts, pins and nails store Y measured from the top edge (max_y - y)."""
    parts = [p for p in d['parts'] if p['pads']]   # pad-less placeholders can't be expressed
    pts = [p for o in d['outline'] for p in o] or [[p['bbox'][i], p['bbox'][j]] for p in parts for i, j in ((0, 1), (2, 3))]
    minx, maxx = min(p[0] for p in pts), max(p[0] for p in pts)
    maxy = max(p[1] for p in pts)
    mil = lambda mm: int(round(mm / 0.0254))
    X = lambda x: mil(x - minx)
    H = mil(maxy - min(p[1] for p in pts))

    def Y(y, side):   # Y up from the bottom edge; bottom-side items are stored flipped back (viewer re-flips them)
        yu = mil(maxy - y)
        return H - yu if side == 'B' else yu

    lines = ['0']
    ring = d['outline'][0] + d['outline'][0][:1] if d['outline'] else []
    lines.append('BRDOUT: %d %d %d' % (len(ring), X(maxx), H))
    lines += ['%d %d' % (X(x), Y(y, 'F')) for x, y in ring]
    lines.append('')

    names = sorted({q[7] for p in parts for q in p['pads'] if q[7]})
    code = {n: i + 1 for i, n in enumerate(names)}
    lines.append('NETS: %d' % len(names))
    lines += ['%d %s' % (code[n], n.replace(' ', '_')) for n in names]
    lines.append('')

    seen, pins, nails = {}, [], []
    lines.append('PARTS: %d' % len(parts))
    for p in parts:
        ref = p['ref'].replace(' ', '_') or '?'
        seen[ref] = seen.get(ref, 0) + 1          # part names must be unique (REF**, R? ...)
        name = ref if seen[ref] == 1 else '%s_%d' % (ref, seen[ref])
        s, b = p['side'], p['bbox']
        lines.append('%s %d %d %d %d %d %d' % (name, X(b[0]), Y(b[3], s), X(b[2]), Y(b[1], s), len(pins), 1 if s == 'F' else 2))
        for q in p['pads']:
            pins.append('%d %d %d %d' % (X(q[2]), Y(q[3], s), code.get(q[7], 0), 1 if s == 'F' else 2))
            if len(p['pads']) == 1 and q[7] and ref.upper().startswith('TP'):
                nails.append((X(q[2]), Y(q[3], s), code[q[7]], 1 if s == 'F' else 2))
    lines.append('')
    lines.append('PINS: %d' % len(pins))
    lines += pins
    lines.append('')
    lines.append('NAILS: %d' % len(nails))
    lines += ['%d %d %d %d %d' % (i, *n) for i, n in enumerate(nails, 1)]
    lines.append('')
    summary = 'outline pts %d, nets %d, parts %d, pins %d, nails %d' % (len(ring), len(names), len(parts), len(pins), len(nails))
    return '\n'.join(lines), summary


def write(path, text):
    with open(path, 'w', encoding='utf-8', newline='\n') as f:
        f.write(text)


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description='Export a .kicad_pcb to an HTML boardview and an OpenBoardView .brd')
    ap.add_argument('pcb')
    ap.add_argument('-o', '--out', help='output directory (default: <board dir>/boardview)')
    ap.add_argument('--no-photo', action='store_true', help='leave out the embedded reference photos')
    ap.add_argument('--kicad-nets-only', action='store_true', help="don't derive nets from copper geometry")
    a = ap.parse_args()
    pcb = os.path.abspath(a.pcb)
    out = a.out or os.path.join(os.path.dirname(pcb), 'boardview')
    os.makedirs(out, exist_ok=True)
    d = load(pcb, derive=not a.kicad_nets_only)
    for name, (text, summary) in ((d['title'] + '-boardview.html', build_html(d, photo=not a.no_photo)),
                                  (d['title'] + '.brd', build_brd(d))):
        write(os.path.join(out, name), text)
        print('%s: %s' % (os.path.join(out, name), summary))
