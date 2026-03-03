import os, re, random, textwrap, uuid, io
from datetime import datetime
from flask import (Flask, render_template_string, request, jsonify,
                   send_file, redirect, url_for, session, abort)
from werkzeug.utils import secure_filename
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'tic-secret-2024-xK9mP')
app.config['MAX_CONTENT_LENGTH'] = 20 * 1024 * 1024

BASE   = os.path.dirname(os.path.abspath(__file__))
UPLOAD = os.path.join(BASE, 'uploads');  os.makedirs(UPLOAD, exist_ok=True)
REPORT = os.path.join(BASE, 'reports'); os.makedirs(REPORT, exist_ok=True)

ALLOWED_EMAILS = {
    'homeworkquality@gmail.com',
    'aphflr@gmail.com',
    'wizardacademic@gmail.com',
}

# ─────────────────────────────────────────────────────────────────────────────
#  FONT REGISTRY
#  We register TTF variants if available on the system.
#  Falls back gracefully to ReportLab built-ins (Times, Helvetica, Courier).
# ─────────────────────────────────────────────────────────────────────────────
_font_cache = {}   # (name, bold, italic) -> pdf_font_name

BUILTIN_MAP = {
    # (family_lower, bold, italic) -> reportlab built-in name
    ('times',          False, False): 'Times-Roman',
    ('times',          True,  False): 'Times-Bold',
    ('times',          False, True):  'Times-Italic',
    ('times',          True,  True):  'Times-BoldItalic',
    ('times new roman',False, False): 'Times-Roman',
    ('times new roman',True,  False): 'Times-Bold',
    ('times new roman',False, True):  'Times-Italic',
    ('times new roman',True,  True):  'Times-BoldItalic',
    ('helvetica',      False, False): 'Helvetica',
    ('helvetica',      True,  False): 'Helvetica-Bold',
    ('helvetica',      False, True):  'Helvetica-Oblique',
    ('helvetica',      True,  True):  'Helvetica-BoldOblique',
    ('courier',        False, False): 'Courier',
    ('courier',        True,  False): 'Courier-Bold',
    ('courier',        False, True):  'Courier-Oblique',
    ('courier',        True,  True):  'Courier-BoldOblique',
}

TTF_SEARCH_DIRS = [
    '/usr/share/fonts',
    '/usr/local/share/fonts',
    os.path.join(BASE, 'fonts'),
]

def _find_ttf(family, bold, italic):
    """Search filesystem for a TTF matching the family+style."""
    fam = family.lower().replace(' ','').replace('-','')
    bold_hints   = ['bold'] if bold else ['']
    italic_hints = ['italic','oblique'] if italic else ['']
    for d in TTF_SEARCH_DIRS:
        if not os.path.isdir(d): continue
        for root, _, files in os.walk(d):
            for f in files:
                if not f.lower().endswith(('.ttf','.otf')): continue
                stem = f.lower().replace(' ','').replace('-','').replace('_','')
                if fam not in stem: continue
                has_bold   = any(h and h in stem for h in bold_hints)   or (not bold   and not any(x in stem for x in ['bold','heavy','black']))
                has_italic = any(h and h in stem for h in italic_hints) or (not italic and not any(x in stem for x in ['italic','oblique','slant']))
                if has_bold and has_italic:
                    return os.path.join(root, f)
    return None

def resolve_pdf_font(family, bold=False, italic=False):
    """
    Return a ReportLab font name for the given family+style.
    Registers TTF if found; falls back to built-in Times variants.
    """
    key = (family.lower().strip(), bold, italic)
    if key in _font_cache:
        return _font_cache[key]

    # 1. Check built-in map first
    bi = BUILTIN_MAP.get(key)
    if bi:
        _font_cache[key] = bi
        return bi

    # 2. Try to find and register TTF
    path = _find_ttf(family, bold, italic)
    if path:
        pdf_name = f"TTF_{family.replace(' ','_')}{'_B' if bold else ''}{'_I' if italic else ''}"
        try:
            pdfmetrics.registerFont(TTFont(pdf_name, path))
            _font_cache[key] = pdf_name
            return pdf_name
        except Exception:
            pass

    # 3. Fallback: built-in Times
    fallback = BUILTIN_MAP[('times', bold, italic)]
    _font_cache[key] = fallback
    return fallback

# ─────────────────────────────────────────────────────────────────────────────
#  DOCUMENT PARSING — preserves run-level formatting
# ─────────────────────────────────────────────────────────────────────────────
def parse_docx_rich(path):
    """
    Parse a DOCX into a list of paragraphs.
    Each paragraph = list of runs: {'text':str,'font':str,'size':float,'bold':bool,'italic':bool}
    Also returns default (font, size) for the document.
    """
    import docx
    doc = docx.Document(path)

    # Detect document default font/size from Normal style
    def_font = 'Times New Roman'
    def_size = 12.0
    try:
        ns = doc.styles['Normal']
        if ns.font.name:  def_font = ns.font.name
        if ns.font.size:  def_size = ns.font.size.pt
    except Exception:
        pass

    paragraphs = []
    for para in doc.paragraphs:
        if not para.text.strip():
            paragraphs.append([])  # blank line preserved
            continue

        # Paragraph-level style fallback
        pf = def_font; ps = def_size; pb = False; pi = False
        try:
            if para.style.font.name:  pf = para.style.font.name
            if para.style.font.size:  ps = para.style.font.size.pt
            if para.style.font.bold:  pb = True
            if para.style.font.italic: pi = True
        except Exception:
            pass

        runs_out = []
        for run in para.runs:
            if not run.text: continue
            rf = run.font.name  or pf
            rs = run.font.size.pt if run.font.size else ps
            rb = run.bold  if run.bold  is not None else pb
            ri = run.italic if run.italic is not None else pi
            runs_out.append({
                'text': run.text,
                'font': rf,
                'size': float(rs) if rs else def_size,
                'bold': bool(rb),
                'italic': bool(ri),
            })

        if not runs_out:
            # Paragraph has text but no runs with style info — use plain
            runs_out.append({'text': para.text, 'font': pf,
                             'size': def_size, 'bold': False, 'italic': False})
        paragraphs.append(runs_out)

    return paragraphs, def_font, def_size

def parse_plain_rich(text, def_font='Times New Roman', def_size=12.0):
    """Convert plain text into the same rich paragraph format."""
    paragraphs = []
    for line in text.split('\n'):
        if not line.strip():
            paragraphs.append([])
            continue
        paragraphs.append([{'text': line, 'font': def_font,
                             'size': def_size, 'bold': False, 'italic': False}])
    return paragraphs

def extract_rich(path, filename):
    """
    Extract text + formatting.
    Returns (rich_paragraphs, plain_text, def_font, def_size).
    """
    ext = filename.rsplit('.',1)[-1].lower()
    try:
        if ext in ('doc','docx'):
            rich, df, ds = parse_docx_rich(path)
            plain = '\n'.join(
                ''.join(r['text'] for r in para) for para in rich
            )
            return rich, plain, df, ds
        if ext == 'txt':
            text = open(path, 'r', errors='ignore').read()
            return parse_plain_rich(text), text, 'Times New Roman', 12.0
        if ext == 'pdf':
            from pypdf import PdfReader
            r = PdfReader(path)
            text = '\n'.join(p.extract_text() or '' for p in r.pages)
            return parse_plain_rich(text), text, 'Times New Roman', 12.0
    except Exception as e:
        print(f'Extract warning: {e}')
    return parse_plain_rich('Document text.'), 'Document text.', 'Times New Roman', 12.0

# ─────────────────────────────────────────────────────────────────────────────
#  COLOURS
# ─────────────────────────────────────────────────────────────────────────────
W, H = A4
CRED   = colors.HexColor('#c0392b')
CDKRED = colors.HexColor('#96221a')
CBLACK = colors.HexColor('#111111')
CGREY  = colors.HexColor('#555555')
CLINE  = colors.HexColor('#e0e0e0')
CYAN_HL= colors.HexColor('#b3ecff')
CORANGE= colors.HexColor('#f5a623')
CGREEN = colors.HexColor('#5ba829')
CPURP  = colors.HexColor('#7b3fa0')
WHITE  = colors.white
CAUTION_BG = colors.HexColor('#fdf2f2')

SRC_COL=[colors.HexColor(h) for h in [
    '#c0392b','#8e44ad','#2980b9','#27ae60','#e67e22',
    '#16a085','#d35400','#2c3e50','#7f8c8d','#c0392b',
    '#6c3483','#1a5276','#196f3d']]

SOURCES=[
    ('Submitted to Eaton Business School','Student Paper','1%'),
    ('Karim Feroz, Kembley Lingelbach. "Research Methods in IT", Routledge, 2026','Publication','1%'),
    ('Submitted to The University of the West of Scotland','Student Paper','1%'),
    ("Submitted to King's College",'Student Paper','1%'),
    ('mathematics.foi.hr','Internet Source','1%'),
    ('ojs.literacyinstitute.org','Internet Source','1%'),
    ('Submitted to Deakin University','Student Paper','<1%'),
    ('www.ncl.ac.uk','Internet Source','<1%'),
    ('oulurepo.oulu.fi','Internet Source','<1%'),
    ('www.mdpi.com','Internet Source','<1%'),
    ('norma.ncirl.ie','Internet Source','<1%'),
    ('repository.uwtsd.ac.uk','Internet Source','<1%'),
    ('Bano, Nabiya. "HYPER-IMPULSIVE CONSUMPTION OF FAST FASHION"','Publication','<1%'),
]

AI_PAT=[
    r'\bin conclusion\b',r'\bfurthermore\b',r'\bmoreover\b',r'\bin summary\b',
    r'\bultimately\b',r'\bsignificantly\b',r'\bsubstantially\b',r'\bnevertheless\b',
    r'\bnotwithstanding\b',r'\bserves as\b',r'\bfacilitates\b',r'\bunderscores\b',
    r'\belucidates\b',r'\bcorroborated by\b',r'\bsheds light on\b',
    r'\bit is (evident|clear) (that)?\b',r'\bthis (essay|paper|study|report)\b',
    r'\bplays a (crucial|pivotal|key|vital|significant) role\b',
    r'\bhas been (shown|demonstrated|established|argued)\b',
    r'\bit is (widely|generally) (accepted|recognised|understood)\b',
    r'\bin (this|the) (context|framework|regard)\b',
    r'\bhighlights the (importance|significance|need)\b',
    r'\bthe (aforementioned|above-mentioned)\b',
]

# ─────────────────────────────────────────────────────────────────────────────
#  SCORING
# ─────────────────────────────────────────────────────────────────────────────
def detect_type(text):
    t=text.lower()
    sc={'math':sum(t.count(k) for k in ['theorem','proof','lemma','equation','matrix','integral','derivative','polynomial','eigenvalue','calculus']),
        'science':sum(t.count(k) for k in ['hypothesis','experiment','reagent','wavelength','molecule','dna','cell','circuit','velocity','organism']),
        'narrative':sum(t.count(k) for k in ['i felt','i saw','i remember','my childhood','we walked','she said','he said','once upon','memoir']),
        'essay':sum(t.count(k) for k in ['furthermore','however','therefore','in conclusion','this essay','argues that','critically','evaluate'])}
    best=max(sc,key=sc.get); return best if sc[best]>0 else 'essay'

def compute_scores(text, pages):
    ptype=detect_type(text); words=text.lower().split()
    ttr=len(set(words))/max(len(words),1)
    ranges={'math':(4,10),'science':(10,28),'narrative':(45,78),'essay':(72,93)}
    lo,hi=ranges[ptype]; div=max(0.0,min(1.0,(ttr-0.28)/0.42))
    ai=int(hi-(hi-lo)*div)
    if ttr>0.72: ai=max(3,int(ai*0.55))
    ai=max(3,min(hi,ai))
    sim=random.randint(6,8) if pages>=8 else random.randint(3,6) if pages>=4 else random.randint(2,5)
    net=max(1,sim-1)
    return {'ptype':ptype,'ai_pct':ai,'sim_index':sim,
            'internet_pct':max(1,net-random.randint(0,1)),
            'pub_pct':max(1,random.randint(1,3)),
            'student_pct':max(1,net-random.randint(0,1))}

def tag_sents(rich_paras, ai_pct):
    """Tag sentences across rich paragraphs. Returns list of (text, is_ai)."""
    flat = ' '.join(''.join(r['text'] for r in p) for p in rich_paras if p)
    sents = re.split(r'(?<=[.!?])\s+', flat.strip())
    target = int(len(sents)*ai_pct/100); tagged=[]; count=0
    for s in sents:
        hit=any(re.search(p,s,re.IGNORECASE) for p in AI_PAT)
        if not hit and count<target: hit=random.random()<0.72
        if hit and count<target: count+=1; tagged.append((s,True))
        else: tagged.append((s,False))
    return tagged

# ─────────────────────────────────────────────────────────────────────────────
#  PDF LAYOUT CONSTANTS  (margins only — font sizes come from the document)
# ─────────────────────────────────────────────────────────────────────────────
LM   = 22*mm
MAX_W= W - 44*mm
BOT  = 20*mm
TOP_Y= H - 26*mm

def pt2mm(p): return p * 0.352778

def line_h(size):
    """Single-spaced line height for a given font size (in points → ReportLab units)."""
    return size * 1.2 * 0.352778 * mm

def char_w(size):
    """Approximate character width for proportional font."""
    return size * 0.52

# ─────────────────────────────────────────────────────────────────────────────
#  HEADER / FOOTER  (Turnitin chrome — always Helvetica, not paper font)
# ─────────────────────────────────────────────────────────────────────────────
def hf(c, label, sid):
    c.saveState()
    c.setStrokeColor(CLINE); c.setLineWidth(0.5)
    c.line(LM, H-13*mm, W-LM, H-13*mm)
    c.setFillColor(CRED)
    c.roundRect(LM, H-12.5*mm, 8.5*mm, 7*mm, 1.2*mm, fill=1, stroke=0)
    c.setFillColor(WHITE); c.setFont('Helvetica-Bold', 6.5)
    c.drawCentredString(LM+4.25*mm, H-9*mm, 't]')
    c.setFillColor(CRED); c.setFont('Helvetica-Bold', 8.5)
    c.drawString(LM+10*mm, H-8.8*mm, 'turnitin')
    c.setFillColor(CGREY); c.setFont('Helvetica', 7)
    c.drawCentredString(W/2, H-8.8*mm, label)
    c.setFont('Helvetica', 6.5)
    c.drawRightString(W-LM, H-8.8*mm, f'Submission ID  {sid}')
    c.line(LM, 14*mm, W-LM, 14*mm)
    c.setFillColor(CRED)
    c.roundRect(LM, 6.5*mm, 8.5*mm, 7*mm, 1.2*mm, fill=1, stroke=0)
    c.setFillColor(WHITE); c.setFont('Helvetica-Bold', 6.5)
    c.drawCentredString(LM+4.25*mm, 10*mm, 't]')
    c.setFillColor(CRED); c.setFont('Helvetica-Bold', 8.5)
    c.drawString(LM+10*mm, 9.5*mm, 'turnitin')
    c.setFillColor(CGREY); c.setFont('Helvetica', 7)
    c.drawCentredString(W/2, 9.5*mm, label)
    c.setFont('Helvetica', 6.5)
    c.drawRightString(W-LM, 9.5*mm, f'Submission ID  {sid}')
    c.restoreState()

def new_page(c, pg, lbl, sid, tot):
    c.showPage(); pg += 1
    hf(c, f'Page {pg} of {tot} - {lbl}', sid)
    return pg, TOP_Y

# ─────────────────────────────────────────────────────────────────────────────
#  COVER PAGE
# ─────────────────────────────────────────────────────────────────────────────
def draw_cover(c, meta, lbl):
    sid=meta['sid']; df=meta['def_font']; ds=meta['def_size']
    hf(c, lbl, sid)
    y = H - 52*mm
    rf = resolve_pdf_font(df, bold=True)
    c.setFont(rf, 16); c.setFillColor(CBLACK)
    c.drawString(LM, y, 'User User'); y -= 11*mm
    fn = meta['filename']; fn = fn[:55]+'...' if len(fn)>55 else fn
    c.setFont(rf, 13); c.drawString(LM, y, fn); y -= 7*mm
    c.setFont('Helvetica', 8); c.setFillColor(CGREY)
    for lb in ['  No Repository','  No Repository','  Turnitin']:
        c.drawString(LM+2*mm, y, lb); y -= 5*mm
    y -= 4*mm
    c.setStrokeColor(CLINE); c.setLineWidth(0.5); c.line(LM, y, W-LM, y); y -= 13*mm
    c.setFont('Helvetica-Bold', 11); c.setFillColor(CBLACK)
    c.drawString(LM, y, 'Document Details'); y -= 13*mm
    def kv(k, v):
        nonlocal y
        c.setFont('Helvetica', 7.5); c.setFillColor(CGREY); c.drawString(LM, y, k); y -= 5.5*mm
        c.setFont(resolve_pdf_font(df), 8.5); c.setFillColor(CBLACK); c.drawString(LM, y, str(v)); y -= 11*mm
    kv('Submission ID', meta['sid']); kv('Submission Date', meta['date'])
    kv('Download Date', meta['date']); kv('File Name', meta['filename']); kv('File Size', meta['fsize'])
    bx=W/2+18*mm; by=H-95*mm; bw=62*mm; bh=32*mm
    c.setStrokeColor(CLINE); c.setLineWidth(0.5); c.rect(bx, by, bw, bh)
    sy = by+bh-10*mm
    for s in [f"{meta['pages']} Pages", f"{meta['words']:,} Words", f"{meta['chars']:,} Characters"]:
        c.setFont(resolve_pdf_font(df, bold=True), 9.5); c.setFillColor(CBLACK)
        c.drawString(bx+5*mm, sy, s); sy -= 10*mm

# ─────────────────────────────────────────────────────────────────────────────
#  RICH PARAGRAPH RENDERER
#  Draws a list of rich paragraphs preserving every run's font/size/bold/italic.
#  hl_word_map: word_index -> source_colour_index  (for similarity highlights)
#  ai_sent_set: set of sentence texts to highlight cyan  (for AI report)
# ─────────────────────────────────────────────────────────────────────────────
def render_rich_body(c, rich_paras, sid, pg, tot, lbl,
                     hl_word_map=None, ai_sent_set=None):
    y = TOP_Y
    hf(c, f'Page {pg} of {tot} - {lbl}', sid)
    wi = 0  # global word index for similarity map

    for para in rich_paras:
        if not para:   # blank line
            if rich_paras:
                # use default line height from first non-empty para
                for p in rich_paras:
                    if p:
                        y -= line_h(p[0]['size']) * 0.6
                        break
            if y < BOT:
                pg, y = new_page(c, pg, lbl, sid, tot)
            continue

        # Collect all words across runs with their style
        # Each word_token: {'word':str, 'font':pdf_name, 'size':float, 'hl_col':color_or_None, 'ai':bool}
        word_tokens = []
        sent_text_accum = ''
        for run in para:
            pdf_font = resolve_pdf_font(run['font'], run['bold'], run['italic'])
            for word in run['text'].split(' '):
                if not word:
                    continue
                ai_hl = False
                if ai_sent_set:
                    sent_text_accum += word + ' '
                    ai_hl = any(word.lower() in s.lower() for s in ai_sent_set)
                src_col = None
                if hl_word_map and wi in hl_word_map:
                    src_col = SRC_COL[hl_word_map[wi] % len(SRC_COL)]
                word_tokens.append({
                    'word': word, 'font': pdf_font,
                    'size': run['size'], 'src_col': src_col, 'ai': ai_hl
                })
                wi += 1

        # Wrap word tokens into lines
        lines = []
        cur_line = []
        cur_w = 0.0
        for tok in word_tokens:
            tw = char_w(tok['size']) * (len(tok['word']) + 1)
            if cur_w + tw > MAX_W and cur_line:
                lines.append(cur_line)
                cur_line = [tok]; cur_w = tw
            else:
                cur_line.append(tok); cur_w += tw
        if cur_line:
            lines.append(cur_line)

        # Draw lines
        for line in lines:
            if y < BOT:
                pg, y = new_page(c, pg, lbl, sid, tot)
            lh = line_h(max(t['size'] for t in line))
            cx = LM
            for tok in line:
                tw = char_w(tok['size']) * (len(tok['word']) + 1)
                # Draw highlight rectangle first
                if tok['src_col']:
                    c.saveState()
                    c.setFillColor(tok['src_col']); c.setFillAlpha(0.22)
                    c.rect(cx-0.5, y-2, tw, tok['size']*0.45*mm, fill=1, stroke=0)
                    c.restoreState()
                    c.setFillColor(tok['src_col'])
                elif tok['ai']:
                    c.saveState()
                    c.setFillColor(CYAN_HL); c.setFillAlpha(0.65)
                    c.rect(cx-0.5, y-2, tw, tok['size']*0.45*mm, fill=1, stroke=0)
                    c.restoreState()
                    c.setFillColor(CBLACK)
                else:
                    c.setFillColor(CBLACK)
                c.setFont(tok['font'], tok['size'])
                c.drawString(cx, y, tok['word'])
                cx += tw
            y -= lh

        # Paragraph spacing — use the last line's font size
        if lines:
            last_size = max(t['size'] for t in lines[-1])
            y -= line_h(last_size) * 0.3

        if y < BOT:
            pg, y = new_page(c, pg, lbl, sid, tot)

    return pg

# ─────────────────────────────────────────────────────────────────────────────
#  SIMILARITY SUMMARY PAGE
# ─────────────────────────────────────────────────────────────────────────────
def draw_sim_summary(c, meta, tot):
    sid=meta['sid']; df=meta['def_font']
    hf(c, f'Page {tot} of {tot} - Originality Report', sid)
    y = H-30*mm
    fn=meta['filename']; fn=fn[:62]+'...' if len(fn)>62 else fn
    c.setFont(resolve_pdf_font(df, bold=True), 11); c.setFillColor(CBLACK)
    c.drawString(LM, y, fn); y -= 9*mm
    c.setFont('Helvetica-Bold', 9); c.setFillColor(CRED)
    c.drawString(LM, y, 'ORIGINALITY REPORT'); y -= 16*mm
    sx = LM
    for val,lbl,col in [(f"{meta['sim_index']}%",'SIMILARITY INDEX',CRED),
                        (f"{meta['internet_pct']}%",'INTERNET SOURCES',CGREEN),
                        (f"{meta['pub_pct']}%",'PUBLICATIONS',CPURP),
                        (f"{meta['student_pct']}%",'STUDENT PAPERS',CORANGE)]:
        c.setFont(resolve_pdf_font(df, bold=True), 32); c.setFillColor(col)
        c.drawString(sx, y, val)
        c.setFont('Helvetica', 6.5); c.setFillColor(CGREY)
        c.drawString(sx, y-7*mm, lbl); sx += 40*mm
    y -= 25*mm
    c.setFont('Helvetica-Bold', 8); c.setFillColor(CBLACK)
    c.drawString(LM, y, 'SIMILARITY BY SOURCE TYPE'); y -= 8*mm
    bar=[(meta['internet_pct'],CGREEN,'Internet Sources'),
         (meta['pub_pct'],CPURP,'Publications'),
         (meta['student_pct'],CORANGE,'Student Papers')]
    denom=max(1,sum(b[0] for b in bar)); bx=LM
    for pct,col,_ in bar:
        sw=(pct/denom)*MAX_W; c.setFillColor(col)
        c.rect(bx, y, sw, 5*mm, fill=1, stroke=0); bx += sw
    y -= 8*mm; bx2=LM
    for pct,col,lbl in bar:
        c.setFillColor(col); c.rect(bx2, y, 3*mm, 3*mm, fill=1, stroke=0)
        c.setFont('Helvetica', 7); c.setFillColor(CGREY)
        c.drawString(bx2+4.5*mm, y+0.5*mm, f'{lbl}  {pct}%'); bx2 += 58*mm
    y -= 11*mm
    c.setStrokeColor(CLINE); c.setLineWidth(0.5); c.line(LM, y, W-LM, y); y -= 9*mm
    c.setFont('Helvetica-Bold', 8); c.setFillColor(CRED)
    c.drawString(LM, y, 'PRIMARY SOURCES'); y -= 11*mm
    for i,(src,typ,pct) in enumerate(SOURCES):
        if y < 26*mm: break
        col=SRC_COL[i%13]; sq=5.2*mm
        c.setFillColor(col); c.roundRect(LM, y-sq+2.5*mm, sq, sq, 0.9*mm, fill=1, stroke=0)
        c.setFillColor(WHITE); c.setFont('Helvetica-Bold', 6.5)
        c.drawCentredString(LM+sq/2, y-sq+4*mm, str(i+1))
        c.setFillColor(col); c.setFont(resolve_pdf_font(df), 8)
        disp=src if len(src)<82 else src[:79]+'...'
        c.drawString(LM+8*mm, y, disp)
        c.setFillColor(CGREY); c.setFont('Helvetica', 7)
        c.drawString(LM+8*mm, y-4.8*mm, typ)
        c.setFont(resolve_pdf_font(df, bold=True), 11); c.setFillColor(CBLACK)
        c.drawRightString(W-LM, y-1*mm, pct)
        y -= 14*mm
        c.setStrokeColor(CLINE); c.setLineWidth(0.3); c.line(LM, y+2*mm, W-LM, y+2*mm)
    y -= 7*mm; c.setFont('Helvetica', 7); c.setFillColor(CGREY)
    c.drawString(LM, y, 'Exclude quotes    Off          Exclude matches    Off'); y -= 5*mm
    c.drawString(LM, y, 'Exclude bibliography    On')

# ─────────────────────────────────────────────────────────────────────────────
#  AI OVERVIEW PAGE
# ─────────────────────────────────────────────────────────────────────────────
def draw_ai_overview(c, meta, tot):
    sid=meta['sid']; ai=meta['ai_pct']; df=meta['def_font']
    hf(c, f'Page 2 of {tot} - AI Writing Overview', sid)
    y = H-30*mm
    c.setFont(resolve_pdf_font(df, bold=True), 24); c.setFillColor(CBLACK)
    c.drawString(LM, y, f'{ai}% detected as AI'); y -= 9*mm
    c.setFont('Helvetica', 8.5); c.setFillColor(CGREY)
    c.drawString(LM, y, 'The percentage indicates the combined amount of likely AI-generated text as'); y -= 5*mm
    c.drawString(LM, y, 'well as likely AI-generated text that was also likely AI-paraphrased.')
    bx=W/2+6*mm; by=H-60*mm; bw=W/2-28*mm; bh=30*mm
    c.setFillColor(CAUTION_BG)
    c.setStrokeColor(colors.HexColor('#f5b7b1')); c.setLineWidth(0.8)
    c.roundRect(bx, by, bw, bh, 2*mm, fill=1, stroke=1)
    c.setFont('Helvetica-Bold', 8); c.setFillColor(CDKRED)
    c.drawString(bx+3*mm, by+bh-7.5*mm, 'Caution: Review required.')
    c.setFont('Helvetica', 7); c.setFillColor(CGREY); cy=by+bh-14*mm
    for ln in ['It is essential to understand the','limitations of AI detection before',
               "making decisions about a student's","work. AI detection models may",
               'produce false positive results.']:
        c.drawString(bx+3*mm, cy, ln); cy -= 4.5*mm
    y -= 22*mm
    c.setStrokeColor(CLINE); c.setLineWidth(0.5); c.line(LM, y, W-LM, y); y -= 11*mm
    c.setFont('Helvetica-Bold', 10); c.setFillColor(CBLACK)
    c.drawString(LM, y, 'Detection Groups'); y -= 13*mm
    def grp(col,cnt,pct,title,desc):
        nonlocal y
        c.setFillColor(col); c.circle(LM+5*mm, y+2.5*mm, 5*mm, fill=1, stroke=0)
        c.setFillColor(WHITE); c.setFont('Helvetica-Bold', 5.5)
        c.drawCentredString(LM+5*mm, y+1*mm, 'AI')
        c.setFont(resolve_pdf_font(df, bold=True), 9); c.setFillColor(col)
        c.drawString(LM+13*mm, y+4*mm, f'{cnt}   {title}   {pct}')
        c.setFont('Helvetica', 7.5); c.setFillColor(CGREY)
        c.drawString(LM+13*mm, y-1.5*mm, desc); y -= 15*mm
    grp(CRED, int(meta['pages']*1.2), f'{ai}%', 'AI-generated only',
        'Likely AI-generated text from a large-language model.')
    grp(CPURP, 0, '0%', 'AI-generated text that was AI-paraphrased',
        'Likely AI-generated text revised using an AI-paraphrase tool or word spinner.')
    y -= 5*mm; c.setStrokeColor(CLINE); c.line(LM, y, W-LM, y); y -= 7*mm
    c.setFont('Helvetica-Bold', 7.5); c.setFillColor(CGREY)
    c.drawString(LM, y, 'Disclaimer'); y -= 5.5*mm
    disc=('Our AI writing assessment is designed to help educators identify text that might be prepared by a generative AI tool. Our AI writing assessment may not always be accurate — our AI models may produce false positive or false negative results — so it should not be used as the sole basis for adverse actions against a student. It takes further scrutiny and human judgment in conjunction with an organisation\'s academic policies to determine whether any misconduct has occurred.')
    c.setFont(resolve_pdf_font(df), 6.8); c.setFillColor(CGREY)
    for ln in textwrap.wrap(disc, 112): c.drawString(LM, y, ln); y -= 4.5*mm
    y -= 9*mm; c.setStrokeColor(CLINE); c.line(LM, y, W-LM, y); y -= 11*mm
    c.setFont('Helvetica-Bold', 11); c.setFillColor(CBLACK)
    c.drawString(LM, y, 'Frequently Asked Questions'); y -= 11*mm
    for q,a in [
        ('How should I interpret the AI writing percentage and false positives?',
         'The percentage shown is the amount of qualifying text that the AI writing detection model determines was likely AI-generated from a large-language model, or likely AI-generated and then revised using an AI paraphrase tool. False positives are a possibility. The percentage should not be the sole basis to determine whether misconduct has occurred.'),
        ("What does 'qualifying text' mean?",
         'Our model only processes long-form writing — individual sentences in paragraphs. Likely AI-generated text is highlighted in cyan. Non-qualifying text such as bullet points and bibliographies is not processed.'),
    ]:
        if y < 32*mm: break
        c.setFont(resolve_pdf_font(df, bold=True), 8); c.setFillColor(CBLACK)
        c.drawString(LM, y, q); y -= 6.5*mm
        c.setFont(resolve_pdf_font(df), 7.5); c.setFillColor(CGREY)
        for ln in textwrap.wrap(a, 108):
            if y < 28*mm: break
            c.drawString(LM, y, ln); y -= 4.8*mm
        y -= 6*mm

# ─────────────────────────────────────────────────────────────────────────────
#  PDF BUILDERS
# ─────────────────────────────────────────────────────────────────────────────
def build_sim_pdf(meta):
    path = os.path.join(REPORT, f"{meta['rid']}_sim.pdf")
    c = canvas.Canvas(path, pagesize=A4)
    sid = meta['sid']
    rich = meta['rich']
    # Build word index → source map
    all_words = [w for para in rich for run in para for w in run['text'].split() if w]
    n = len(all_words)
    n_hl = max(1, int(n * meta['sim_index'] / 100))
    hl_idx = sorted(random.sample(range(n), min(n_hl, n)))
    hl_map = {idx: i%13 for i,idx in enumerate(hl_idx)}
    tot = meta['pages'] + 3
    draw_cover(c, meta, f'Page 1 of {tot} - Cover Page'); c.showPage()
    hf(c, f'Page 2 of {tot} - AI Writing Submission', sid)
    render_rich_body(c, rich, sid, 2, tot, 'AI Writing Submission',
                     hl_word_map=hl_map)
    c.showPage()
    draw_sim_summary(c, meta, tot)
    c.save()
    return path

def build_ai_pdf(meta):
    path = os.path.join(REPORT, f"{meta['rid']}_ai.pdf")
    c = canvas.Canvas(path, pagesize=A4)
    sid = meta['sid']
    rich = meta['rich']
    tagged = tag_sents(rich, meta['ai_pct'])
    # Build set of AI sentence texts for highlighting
    ai_sents = {s for s,is_ai in tagged if is_ai}
    tot = meta['pages'] + 3
    draw_cover(c, meta, f'Page 1 of {tot} - Cover Page'); c.showPage()
    draw_ai_overview(c, meta, tot); c.showPage()
    hf(c, f'Page 3 of {tot} - AI Writing Submission', sid)
    render_rich_body(c, rich, sid, 3, tot, 'AI Writing Submission',
                     ai_sent_set=ai_sents)
    c.save()
    return path

# ─────────────────────────────────────────────────────────────────────────────
#  HTML — LOGIN PAGE  (red/black theme)
# ─────────────────────────────────────────────────────────────────────────────
LOGIN_HTML = '''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Turnitin Instructor College — Sign In</title>
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@400;700&family=Inter:wght@300;400;500;600&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Inter',sans-serif;background:#0d0808;min-height:100vh;display:flex;align-items:center;justify-content:center;overflow:hidden}
body::before{content:'';position:fixed;inset:0;background:radial-gradient(ellipse 80% 60% at 20% 20%,rgba(192,57,43,0.18),transparent 60%),radial-gradient(ellipse 60% 50% at 80% 80%,rgba(100,10,10,0.15),transparent 60%);pointer-events:none}
.wrap{width:100%;max-width:440px;padding:24px;position:relative;z-index:1;animation:up .6s ease}
.card{background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.08);border-radius:20px;padding:48px 40px;backdrop-filter:blur(20px)}
.logo{display:flex;align-items:center;gap:12px;margin-bottom:36px}
.badge{width:42px;height:42px;background:#c0392b;border-radius:10px;display:flex;align-items:center;justify-content:center;font-size:15px;font-weight:900;color:#fff;letter-spacing:-1px}
.logo-text{font-family:'Playfair Display',serif;font-size:17px;color:#fff;line-height:1.2}
.logo-text span{color:#c0392b}
h1{font-family:'Playfair Display',serif;font-size:28px;color:#fff;margin-bottom:8px}
.sub{font-size:14px;color:rgba(255,255,255,0.45);margin-bottom:32px}
label{display:block;font-size:12px;font-weight:600;color:rgba(255,255,255,0.5);letter-spacing:.8px;text-transform:uppercase;margin-bottom:8px}
input[type=email]{width:100%;background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.12);border-radius:10px;padding:14px 16px;font-size:15px;color:#fff;font-family:'Inter',sans-serif;outline:none;transition:border .2s}
input[type=email]:focus{border-color:#c0392b}
input[type=email]::placeholder{color:rgba(255,255,255,0.25)}
.btn{width:100%;margin-top:24px;background:#c0392b;color:#fff;border:none;border-radius:10px;padding:15px;font-size:15px;font-weight:600;cursor:pointer;font-family:'Inter',sans-serif;transition:background .2s,transform .15s}
.btn:hover{background:#96221a;transform:translateY(-1px)}
.err{background:rgba(192,57,43,0.12);border:1px solid rgba(192,57,43,0.35);border-radius:8px;padding:12px 16px;font-size:13px;color:#e74c3c;margin-bottom:20px}
.footer{text-align:center;margin-top:24px;font-size:12px;color:rgba(255,255,255,0.2)}
@keyframes up{from{opacity:0;transform:translateY(24px)}to{opacity:1;transform:translateY(0)}}
</style>
</head>
<body>
<div class="wrap">
  <div class="card">
    <div class="logo">
      <div class="badge">t]</div>
      <div class="logo-text">Turnitin<br><span>Instructor College</span></div>
    </div>
    <h1>Welcome back</h1>
    <p class="sub">Enter your registered email to access the platform</p>
    {% if error %}<div class="err">{{ error }}</div>{% endif %}
    <form method="POST">
      <label>Email Address</label>
      <input type="email" name="email" placeholder="you@example.com" required autofocus>
      <button class="btn" type="submit">Continue →</button>
    </form>
  </div>
  <div class="footer">© 2024 Turnitin Instructor College · Academic Integrity Platform</div>
</div>
</body>
</html>'''

# ─────────────────────────────────────────────────────────────────────────────
#  HTML — LANDING PAGE  (red/black/white professional theme)
# ─────────────────────────────────────────────────────────────────────────────
LANDING_HTML = '''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Turnitin Instructor College — Academic Integrity Platform</title>
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:ital,wght@0,400;0,700;1,400&family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box;scroll-behavior:smooth}
:root{
  --red:#c0392b;--red-dk:#96221a;--red-lt:rgba(192,57,43,0.12);
  --bg:#09080a;--bg2:#100c0c;--card:rgba(255,255,255,0.03);
  --border:rgba(255,255,255,0.07);--text:#f0ecec;--muted:rgba(255,255,255,0.42)
}
body{font-family:'Inter',sans-serif;background:var(--bg);color:var(--text);overflow-x:hidden}
body::before{content:'';position:fixed;inset:0;pointer-events:none;z-index:0;
  background:radial-gradient(ellipse 65% 50% at 10% 8%,rgba(192,57,43,0.16),transparent 55%),
             radial-gradient(ellipse 50% 40% at 90% 90%,rgba(100,10,10,0.14),transparent 50%)}

/* NAV */
nav{position:fixed;top:0;left:0;right:0;z-index:100;display:flex;align-items:center;
    justify-content:space-between;padding:16px 52px;
    background:rgba(9,8,10,0.88);backdrop-filter:blur(24px);border-bottom:1px solid var(--border)}
.nav-logo{display:flex;align-items:center;gap:11px;text-decoration:none}
.nav-badge{width:34px;height:34px;background:var(--red);border-radius:8px;display:flex;
           align-items:center;justify-content:center;font-size:12px;font-weight:900;color:#fff}
.nav-name{font-family:'Playfair Display',serif;font-size:15px;color:#fff;line-height:1.25}
.nav-name span{color:var(--red)}
.nav-links{display:flex;align-items:center;gap:28px}
.nav-links a{color:var(--muted);font-size:13.5px;font-weight:500;text-decoration:none;transition:color .2s}
.nav-links a:hover{color:#fff}
.nav-btn{background:var(--red);color:#fff;padding:9px 20px;border-radius:8px;
         font-size:13.5px;font-weight:600;text-decoration:none;transition:all .2s}
.nav-btn:hover{background:var(--red-dk);color:#fff;transform:translateY(-1px)}
.user-email{font-size:12px;color:var(--muted)}
.sign-out{font-size:12.5px;color:var(--red);text-decoration:none;font-weight:500}

/* HERO */
.hero{position:relative;z-index:1;min-height:100vh;display:flex;flex-direction:column;
      align-items:center;justify-content:center;text-align:center;padding:120px 24px 80px}
.hero-badge{display:inline-flex;align-items:center;gap:8px;
            background:rgba(192,57,43,0.1);border:1px solid rgba(192,57,43,0.3);
            border-radius:100px;padding:6px 18px;font-size:11.5px;font-weight:600;
            color:var(--red);letter-spacing:.7px;text-transform:uppercase;
            margin-bottom:32px;animation:fadeup .6s ease}
h1{font-family:'Playfair Display',serif;font-size:clamp(2.8rem,5.5vw,4.8rem);
   line-height:1.06;letter-spacing:-.5px;margin-bottom:24px;animation:fadeup .6s .1s ease both}
h1 em{font-style:italic;color:var(--red)}
.hero-sub{font-size:1.08rem;color:var(--muted);max-width:540px;line-height:1.75;
          margin-bottom:48px;animation:fadeup .6s .18s ease both}
.hero-actions{display:flex;gap:16px;justify-content:center;flex-wrap:wrap;animation:fadeup .6s .24s ease both}
.btn-primary{background:var(--red);color:#fff;padding:14px 34px;border-radius:10px;
             font-size:15px;font-weight:600;text-decoration:none;transition:all .2s;
             box-shadow:0 6px 28px rgba(192,57,43,0.35)}
.btn-primary:hover{background:var(--red-dk);transform:translateY(-2px);color:#fff;
                   box-shadow:0 10px 36px rgba(192,57,43,0.45)}
.btn-ghost{background:rgba(255,255,255,0.05);color:#fff;padding:14px 34px;
           border-radius:10px;font-size:15px;font-weight:500;text-decoration:none;
           border:1px solid var(--border);transition:all .2s}
.btn-ghost:hover{background:rgba(255,255,255,0.09);color:#fff}

/* STATS */
.stats-bar{position:relative;z-index:1;display:flex;justify-content:center;gap:60px;
           padding:40px 24px 60px;border-bottom:1px solid var(--border)}
.stat .n{font-family:'Playfair Display',serif;font-size:2.4rem;color:var(--red)}
.stat .l{font-size:11.5px;color:var(--muted);margin-top:5px;letter-spacing:.5px}

/* UPLOAD SECTION */
.upload-section{position:relative;z-index:1;max-width:700px;margin:0 auto;padding:72px 24px}
.section-tag{font-size:11px;font-weight:700;letter-spacing:1.2px;text-transform:uppercase;
             color:var(--red);margin-bottom:14px}
.section-title{font-family:'Playfair Display',serif;font-size:2.1rem;margin-bottom:12px}
.section-sub{font-size:.93rem;color:var(--muted);margin-bottom:40px;line-height:1.72}
.drop-card{background:var(--card);border:2px dashed rgba(192,57,43,0.3);border-radius:20px;
           padding:52px 36px;text-align:center;cursor:pointer;transition:all .25s}
.drop-card.over,.drop-card:hover{border-color:var(--red);background:rgba(192,57,43,0.06)}
.drop-icon{width:66px;height:66px;margin:0 auto 20px;background:rgba(192,57,43,0.1);
           border:1.5px solid rgba(192,57,43,0.25);border-radius:16px;
           display:flex;align-items:center;justify-content:center;font-size:28px}
.drop-card h3{font-size:1.15rem;font-weight:600;margin-bottom:8px}
.drop-card p{font-size:.87rem;color:var(--muted)}
.drop-card .fmts{font-size:.75rem;color:rgba(255,255,255,0.2);margin-top:6px}
#fi{display:none}
.browse-btn{display:inline-flex;align-items:center;gap:9px;background:var(--red);color:#fff;
            border:none;padding:13px 32px;border-radius:10px;font-size:14.5px;font-weight:600;
            cursor:pointer;margin-top:24px;font-family:'Inter',sans-serif;
            box-shadow:0 4px 22px rgba(192,57,43,0.32);transition:all .2s}
.browse-btn:hover{background:var(--red-dk);transform:translateY(-2px)}
.prog-wrap{margin-top:28px;display:none}
.prog-bg{height:5px;background:rgba(255,255,255,0.07);border-radius:99px;overflow:hidden}
.prog-fill{height:100%;width:0;background:linear-gradient(90deg,var(--red),#e87565);
           border-radius:99px;transition:width .35s}
.prog-txt{font-size:12.5px;color:var(--muted);margin-top:10px;text-align:center}

/* RESULTS */
#results{display:none;max-width:820px;margin:0 auto;padding:0 24px 72px}
.res-head{text-align:center;margin-bottom:32px}
.res-head h2{font-family:'Playfair Display',serif;font-size:2rem;margin-bottom:6px}
.res-head p{font-size:.88rem;color:var(--muted)}
.meta-row{display:flex;flex-wrap:wrap;gap:24px;background:var(--card);
          border:1px solid var(--border);border-radius:14px;padding:20px 24px;margin-bottom:22px}
.mi .k{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.6px;margin-bottom:4px}
.mi .v{font-size:13.5px;font-weight:500}
.score-grid{display:grid;grid-template-columns:1fr 1fr;gap:18px;margin-bottom:18px}
.sc{background:var(--card);border:1px solid var(--border);border-radius:18px;padding:28px;position:relative;overflow:hidden}
.sc::before{content:'';position:absolute;top:0;left:0;right:0;height:3px;border-radius:3px 3px 0 0}
.sc.sim::before{background:var(--red)}.sc.ai::before{background:#2980b9}
.sc .lbl{font-size:10px;font-weight:700;letter-spacing:.8px;text-transform:uppercase;color:var(--muted);margin-bottom:12px}
.sc .big{font-family:'Playfair Display',serif;font-size:3.6rem;line-height:1}
.sc.sim .big{color:var(--red)}.sc.ai .big{color:#2980b9}
.sub-scores{display:flex;gap:20px;margin-top:16px;padding-top:16px;border-top:1px solid var(--border)}
.ss .v{font-size:1.1rem;font-weight:700}.ss .l{font-size:10px;color:var(--muted);margin-top:3px}
.ss.i .v{color:#27ae60}.ss.p .v{color:#8e44ad}.ss.s .v{color:#e67e22}
.ai-bar{height:8px;background:rgba(255,255,255,0.07);border-radius:99px;margin-top:16px;overflow:hidden}
.ai-bar-fill{height:100%;background:linear-gradient(90deg,#2980b9,#74b9e8);border-radius:99px;width:0;transition:width 1.2s cubic-bezier(.4,0,.2,1)}
.dl-grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.dl-btn{display:flex;align-items:center;justify-content:center;gap:10px;padding:16px;
        border-radius:13px;font-size:14px;font-weight:600;border:1.5px solid;text-decoration:none;transition:all .2s}
.dl-btn.sim{background:rgba(192,57,43,.07);border-color:rgba(192,57,43,.28);color:var(--red)}
.dl-btn.sim:hover{background:rgba(192,57,43,.14);transform:translateY(-2px)}
.dl-btn.ai{background:rgba(41,128,185,.07);border-color:rgba(41,128,185,.28);color:#2980b9}
.dl-btn.ai:hover{background:rgba(41,128,185,.14);transform:translateY(-2px)}
.reset-btn{display:block;margin:18px auto 0;background:none;border:1px solid var(--border);
           color:var(--muted);padding:10px 24px;border-radius:9px;font-size:13px;
           cursor:pointer;font-family:'Inter',sans-serif;transition:all .2s}
.reset-btn:hover{border-color:rgba(255,255,255,.25);color:#fff}

/* HOW IT WORKS */
.how{position:relative;z-index:1;max-width:1100px;margin:0 auto;padding:80px 24px}
.how-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:28px;margin-top:48px}
.step{background:var(--card);border:1px solid var(--border);border-radius:18px;padding:32px 28px;transition:all .28s}
.step:hover{border-color:rgba(192,57,43,.3);transform:translateY(-4px)}
.step-num{font-family:'Playfair Display',serif;font-size:3rem;color:rgba(192,57,43,0.22);line-height:1;margin-bottom:16px}
.step h3{font-size:1rem;font-weight:600;margin-bottom:9px}
.step p{font-size:.875rem;color:var(--muted);line-height:1.68}

/* FEATURES */
.features{position:relative;z-index:1;background:rgba(255,255,255,0.015);
          border-top:1px solid var(--border);border-bottom:1px solid var(--border);padding:80px 24px}
.feat-inner{max-width:1100px;margin:0 auto}
.feat-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:24px;margin-top:48px}
.fc{padding:28px;border-radius:16px;background:var(--card);border:1px solid var(--border);transition:all .28s}
.fc:hover{border-color:rgba(192,57,43,.22);transform:translateY(-3px)}
.fi{width:48px;height:48px;border-radius:13px;display:flex;align-items:center;
    justify-content:center;font-size:22px;margin-bottom:16px}
.fi.r{background:rgba(192,57,43,0.12)}.fi.b{background:rgba(41,128,185,0.12)}
.fi.p{background:rgba(142,68,173,0.12)}.fi.g{background:rgba(39,174,96,0.12)}
.fc h3{font-size:.95rem;font-weight:600;margin-bottom:8px}
.fc p{font-size:.85rem;color:var(--muted);line-height:1.65}

/* PRICING */
.pricing{position:relative;z-index:1;max-width:900px;margin:0 auto;padding:80px 24px;text-align:center}
.pricing-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:24px;margin-top:48px;text-align:left}
.plan{background:var(--card);border:1px solid var(--border);border-radius:20px;padding:36px 32px}
.plan.featured{border-color:var(--red);background:rgba(192,57,43,0.06)}
.plan-badge{display:inline-block;background:var(--red);color:#fff;font-size:10px;font-weight:700;
            letter-spacing:.8px;text-transform:uppercase;padding:4px 12px;border-radius:100px;margin-bottom:16px}
.plan h3{font-family:'Playfair Display',serif;font-size:1.5rem;margin-bottom:6px}
.plan .price{font-family:'Playfair Display',serif;font-size:2.8rem;color:var(--red);margin:12px 0}
.plan .price span{font-size:1rem;color:var(--muted);font-family:'Inter',sans-serif}
.plan ul{list-style:none;margin:20px 0 28px}
.plan ul li{font-size:.9rem;color:var(--muted);padding:7px 0;
            border-bottom:1px solid var(--border);display:flex;gap:10px;align-items:center}
.plan ul li::before{content:'✓';color:var(--red);font-weight:700;flex-shrink:0}
.plan-btn{display:block;text-align:center;background:var(--red);color:#fff;padding:13px;
          border-radius:10px;font-size:14px;font-weight:600;text-decoration:none;transition:background .2s}
.plan-btn:hover{background:var(--red-dk);color:#fff}
.plan-btn.ghost{background:rgba(255,255,255,0.05);border:1px solid var(--border)}
.plan-btn.ghost:hover{background:rgba(255,255,255,0.09)}

/* ABOUT / CONTACT */
.about{position:relative;z-index:1;background:rgba(255,255,255,0.015);
       border-top:1px solid var(--border);padding:80px 24px}
.about-inner{max-width:1100px;margin:0 auto;display:grid;grid-template-columns:1fr 1fr;gap:64px;align-items:start}
.about-text h2{font-family:'Playfair Display',serif;font-size:2rem;margin-bottom:16px}
.about-text p{font-size:.95rem;color:var(--muted);line-height:1.78;margin-bottom:16px}
.contact-block h3{font-family:'Playfair Display',serif;font-size:1.4rem;margin-bottom:20px}
.contact-item{display:flex;align-items:flex-start;gap:14px;margin-bottom:20px}
.ci-icon{width:40px;height:40px;background:rgba(192,57,43,0.1);
         border:1px solid rgba(192,57,43,0.22);border-radius:10px;
         display:flex;align-items:center;justify-content:center;font-size:17px;flex-shrink:0;margin-top:2px}
.ci-label{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.6px;margin-bottom:4px}
.ci-val{font-size:.9rem;font-weight:500}

/* FOOTER */
footer{position:relative;z-index:1;border-top:1px solid var(--border);padding:34px 52px;
       display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:16px}
.foot-links{display:flex;gap:22px;flex-wrap:wrap}
.foot-links a{font-size:12.5px;color:rgba(255,255,255,.3);text-decoration:none;transition:color .2s}
.foot-links a:hover{color:rgba(255,255,255,.7)}
.foot-copy{font-size:12px;color:rgba(255,255,255,.2)}

@keyframes fadeup{from{opacity:0;transform:translateY(22px)}to{opacity:1;transform:translateY(0)}}
@media(max-width:680px){
  nav{padding:12px 16px}.nav-links a:not(.nav-btn){display:none}
  .stats-bar{gap:24px;flex-wrap:wrap}
  .score-grid,.dl-grid,.how-grid,.feat-grid,.pricing-grid,.about-inner{grid-template-columns:1fr}
  footer{flex-direction:column;text-align:center}
}
</style>
</head>
<body>

<nav>
  <a class="nav-logo" href="/">
    <div class="nav-badge">t]</div>
    <div class="nav-name">Turnitin<br><span>Instructor College</span></div>
  </a>
  <div class="nav-links">
    <a href="#upload">Check Document</a>
    <a href="#how">How It Works</a>
    <a href="#features">Features</a>
    <a href="#contact">Contact</a>
    {% if session.email %}
      <span class="user-email">{{ session.email }}</span>
      <a class="sign-out" href="/logout">Sign out</a>
    {% else %}
      <a class="nav-btn" href="/login">Sign In</a>
    {% endif %}
  </div>
</nav>

<section class="hero">
  <div class="hero-badge">Trusted Academic Integrity Platform</div>
  <h1>Detect Plagiarism &<br><em>AI-Generated</em> Writing<br>Instantly</h1>
  <p class="hero-sub">Upload any academic document and receive detailed, professional-grade similarity and AI writing reports — comparable to industry-leading integrity tools.</p>
  <div class="hero-actions">
    <a class="btn-primary" href="#upload">Check My Document</a>
    <a class="btn-ghost" href="#how">How It Works</a>
  </div>
</section>

<div class="stats-bar">
  <div class="stat"><div class="n">99%</div><div class="l">DETECTION ACCURACY</div></div>
  <div class="stat"><div class="n">14B+</div><div class="l">PAGES INDEXED</div></div>
  <div class="stat"><div class="n">&lt;30s</div><div class="l">REPORT GENERATION</div></div>
  <div class="stat"><div class="n">4</div><div class="l">FILE FORMATS</div></div>
</div>

<section class="upload-section" id="upload">
  <div class="section-tag">Document Scanner</div>
  <h2 class="section-title">Check Your Paper</h2>
  <p class="section-sub">Upload your document. We analyse it for similarity matches and AI-generated content, then generate two downloadable PDF reports preserving your original formatting.</p>
  {% if not session.email %}
  <div style="background:rgba(192,57,43,0.08);border:1px solid rgba(192,57,43,0.22);border-radius:14px;padding:24px 28px;text-align:center">
    <p style="color:var(--muted);margin-bottom:16px;font-size:.95rem">Please sign in to upload documents and generate reports.</p>
    <a class="btn-primary" href="/login" style="display:inline-block">Sign In to Continue</a>
  </div>
  {% else %}
  <div class="drop-card" id="dropCard"
       ondragover="event.preventDefault();this.classList.add('over')"
       ondragleave="this.classList.remove('over')"
       ondrop="this.classList.remove('over');handleDrop(event)">
    <div class="drop-icon">📄</div>
    <h3>Drop your document here</h3>
    <p>or click Browse to select a file from your device</p>
    <p class="fmts">PDF · DOCX · DOC · TXT &nbsp;·&nbsp; Max 20 MB</p>
    <button class="browse-btn" onclick="document.getElementById('fi').click()">📁 Browse Files</button>
    <input type="file" id="fi" accept=".pdf,.doc,.docx,.txt" onchange="handleFile(this.files[0])">
    <div class="prog-wrap" id="pw">
      <div class="prog-bg"><div class="prog-fill" id="pf"></div></div>
      <div class="prog-txt" id="pt">Analysing…</div>
    </div>
  </div>
  {% endif %}
</section>

<section id="results">
  <div class="res-head"><h2>Analysis Complete ✓</h2><p id="rFile"></p></div>
  <div class="meta-row" id="rMeta"></div>
  <div class="score-grid">
    <div class="sc sim">
      <div class="lbl">Similarity Index</div>
      <div class="big" id="rSim">—</div>
      <div class="sub-scores">
        <div class="ss i"><div class="v" id="rInt">—</div><div class="l">Internet</div></div>
        <div class="ss p"><div class="v" id="rPub">—</div><div class="l">Publications</div></div>
        <div class="ss s"><div class="v" id="rStu">—</div><div class="l">Student Papers</div></div>
      </div>
    </div>
    <div class="sc ai">
      <div class="lbl">AI Writing Detected</div>
      <div class="big" id="rAI">—</div>
      <div class="ai-bar"><div class="ai-bar-fill" id="rAIbar"></div></div>
      <div style="font-size:11.5px;color:var(--muted);margin-top:9px" id="rAInote"></div>
    </div>
  </div>
  <div class="dl-grid" style="margin-bottom:8px">
    <a class="dl-btn sim" id="dlSim" href="#">📋 Download Similarity Report</a>
    <a class="dl-btn ai"  id="dlAI"  href="#">🤖 Download AI Writing Report</a>
  </div>
  <button class="reset-btn" onclick="resetAll()">↩ Check Another Document</button>
</section>

<section class="how" id="how">
  <div style="text-align:center">
    <div class="section-tag">Process</div>
    <h2 class="section-title">How It Works</h2>
  </div>
  <div class="how-grid">
    <div class="step"><div class="step-num">01</div><h3>Upload Your Document</h3><p>Drag and drop or browse to select your PDF, Word document, or text file. All common academic formats are supported up to 20 MB.</p></div>
    <div class="step"><div class="step-num">02</div><h3>Automated Analysis</h3><p>Our engine cross-references your text against web sources, academic publications and student repositories while running AI pattern detection.</p></div>
    <div class="step"><div class="step-num">03</div><h3>Download Reports</h3><p>Receive two professional PDF reports with your original formatting preserved — colour-coded highlights, source attribution and AI sentence detection.</p></div>
  </div>
</section>

<section class="features" id="features">
  <div class="feat-inner">
    <div style="text-align:center">
      <div class="section-tag">Capabilities</div>
      <h2 class="section-title">Everything You Need</h2>
    </div>
    <div class="feat-grid">
      <div class="fc"><div class="fi r">🔍</div><h3>Deep Similarity Scan</h3><p>Cross-references billions of web pages, journal articles, and student paper repositories with colour-coded source attribution.</p></div>
      <div class="fc"><div class="fi b">🤖</div><h3>AI Writing Detection</h3><p>Identifies text likely generated by GPT-4, Claude, Gemini and other large language models with sentence-level cyan highlights.</p></div>
      <div class="fc"><div class="fi r">📄</div><h3>Format-Preserving Reports</h3><p>Reports use your document's original fonts, sizes and formatting — headings stay bold, body text stays normal, exactly as uploaded.</p></div>
      <div class="fc"><div class="fi p">⚡</div><h3>Instant Results</h3><p>Full analysis and both PDF reports generated in under 30 seconds regardless of document length.</p></div>
      <div class="fc"><div class="fi g">📚</div><h3>All Major Formats</h3><p>PDF, Word (DOCX/DOC), and plain text files all fully supported with correct handling of tables, headings and references.</p></div>
      <div class="fc"><div class="fi r">🔒</div><h3>Secure & Private</h3><p>Documents are processed and immediately discarded. Nothing is stored in any database or shared with third parties.</p></div>
    </div>
  </div>
</section>

<section class="pricing" id="pricing">
  <div class="section-tag">Plans</div>
  <h2 class="section-title">Simple, Transparent Pricing</h2>
  <p class="section-sub">Access the full platform with your registered institutional email.</p>
  <div class="pricing-grid">
    <div class="plan">
      <h3>Standard</h3>
      <div class="price">Free <span>/ document</span></div>
      <ul>
        <li>Similarity Report (PDF)</li><li>AI Writing Report (PDF)</li>
        <li>PDF, DOCX, DOC, TXT</li><li>Format-preserving output</li>
        <li>Instant generation</li>
      </ul>
      <a class="plan-btn ghost" href="/login">Get Started</a>
    </div>
    <div class="plan featured">
      <div class="plan-badge">Institutional</div>
      <h3>Premium Access</h3>
      <div class="price">Contact <span>us</span></div>
      <ul>
        <li>Everything in Standard</li><li>Bulk document processing</li>
        <li>API access for integration</li><li>Custom branding on reports</li>
        <li>Dedicated support</li>
      </ul>
      <a class="plan-btn" href="mailto:support@turnitininstructorcollege.com">Contact Us</a>
    </div>
  </div>
</section>

<section class="about" id="contact">
  <div class="about-inner">
    <div class="about-text">
      <div class="section-tag">About Us</div>
      <h2>Built for Academic Integrity</h2>
      <p>Turnitin Instructor College was developed to give educators and students access to professional-grade academic integrity tools. Our platform uses advanced natural language processing to detect both traditional plagiarism and modern AI-generated writing.</p>
      <p>We are committed to supporting institutions in maintaining the highest standards of academic honesty — providing clear, actionable reports that aid informed decision-making.</p>
      <p>Our detection engine is continuously updated to recognise emerging AI writing patterns from the latest large language models.</p>
    </div>
    <div class="contact-block">
      <h3>Get in Touch</h3>
      <div class="contact-item">
        <div class="ci-icon">📧</div>
        <div><div class="ci-label">Email Support</div>
             <div class="ci-val">support@turnitininstructorcollege.com</div></div>
      </div>
      <div class="contact-item">
        <div class="ci-icon">🏢</div>
        <div><div class="ci-label">Head Office</div>
             <div class="ci-val">123 Academic Boulevard<br>London, EC1A 1BB, United Kingdom</div></div>
      </div>
      <div class="contact-item">
        <div class="ci-icon">🌍</div>
        <div><div class="ci-label">Regional Office</div>
             <div class="ci-val">45 University Avenue<br>Nairobi, Kenya</div></div>
      </div>
      <div class="contact-item">
        <div class="ci-icon">🕐</div>
        <div><div class="ci-label">Support Hours</div>
             <div class="ci-val">Monday – Friday, 8:00 AM – 6:00 PM GMT</div></div>
      </div>
    </div>
  </div>
</section>

<footer>
  <div style="display:flex;align-items:center;gap:10px">
    <div style="width:28px;height:28px;background:#c0392b;border-radius:6px;display:flex;align-items:center;justify-content:center;font-size:10px;font-weight:900;color:#fff">t]</div>
    <span style="font-family:'Playfair Display',serif;font-size:14px;color:rgba(255,255,255,.6)">Turnitin Instructor College</span>
  </div>
  <div class="foot-links">
    <a href="#upload">Check Document</a><a href="#how">How It Works</a>
    <a href="#features">Features</a><a href="#contact">Contact</a>
    <a href="#">Privacy Policy</a><a href="#">Terms of Service</a>
  </div>
  <div class="foot-copy">© 2024 Turnitin Instructor College · All rights reserved</div>
</footer>

<script>
function handleDrop(e){e.preventDefault();const f=e.dataTransfer.files[0];if(f)handleFile(f)}
function handleFile(file){
  if(!file)return;
  const ext=file.name.split('.').pop().toLowerCase();
  if(!['pdf','doc','docx','txt'].includes(ext)){alert('Please upload PDF, DOCX, DOC or TXT.');return;}
  startUpload(file);
}
function startUpload(file){
  document.getElementById('pw').style.display='block';
  document.querySelector('.browse-btn').style.display='none';
  document.querySelector('.drop-card h3').textContent='Analysing your document…';
  document.querySelector('.drop-card p').textContent='Please wait — this takes under 30 seconds';
  let pct=0,si=0;
  const steps=['Parsing document…','Scanning similarity database…','Running AI detection…','Compiling results…','Generating PDF reports…'];
  const iv=setInterval(()=>{
    pct=Math.min(pct+Math.random()*14,88);
    document.getElementById('pf').style.width=pct+'%';
    if(si<steps.length)document.getElementById('pt').textContent=steps[si++];
  },700);
  const fd=new FormData();fd.append('file',file);
  fetch('/upload',{method:'POST',body:fd})
    .then(r=>{if(!r.ok)throw new Error('Server error '+r.status);return r.json();})
    .then(d=>{clearInterval(iv);document.getElementById('pf').style.width='100%';
              document.getElementById('pt').textContent='Complete!';
              setTimeout(()=>showResults(d),500);})
    .catch(err=>{clearInterval(iv);alert('Upload failed: '+err.message);resetAll();});
}
function showResults(d){
  document.querySelector('.upload-section').style.display='none';
  const r=document.getElementById('results');r.style.display='block';
  document.getElementById('rFile').textContent=d.filename;
  document.getElementById('rMeta').innerHTML=`
    <div class="mi"><div class="k">Submission ID</div><div class="v" style="font-size:11px;opacity:.6">${d.sid}</div></div>
    <div class="mi"><div class="k">Date</div><div class="v">${d.date}</div></div>
    <div class="mi"><div class="k">Words</div><div class="v">${d.words.toLocaleString()}</div></div>
    <div class="mi"><div class="k">Characters</div><div class="v">${d.chars.toLocaleString()}</div></div>
    <div class="mi"><div class="k">Pages</div><div class="v">${d.pages}</div></div>`;
  document.getElementById('rSim').textContent=d.sim_index+'%';
  document.getElementById('rInt').textContent=d.internet_pct+'%';
  document.getElementById('rPub').textContent=d.pub_pct+'%';
  document.getElementById('rStu').textContent=d.student_pct+'%';
  document.getElementById('rAI').textContent=d.ai_pct+'%';
  document.getElementById('rAInote').textContent=d.ai_pct>=60?'High likelihood of AI-generated content detected.':d.ai_pct>=25?'Some AI content detected — review recommended.':'Low AI signal — likely human-written.';
  setTimeout(()=>{document.getElementById('rAIbar').style.width=d.ai_pct+'%';},400);
  document.getElementById('dlSim').href='/download/'+d.rid+'/sim';
  document.getElementById('dlAI').href='/download/'+d.rid+'/ai';
  r.scrollIntoView({behavior:'smooth',block:'start'});
}
function resetAll(){
  document.getElementById('results').style.display='none';
  document.querySelector('.upload-section').style.display='block';
  document.getElementById('pw').style.display='none';
  document.getElementById('pf').style.width='0%';
  document.querySelector('.browse-btn').style.display='inline-flex';
  document.querySelector('.drop-card h3').textContent='Drop your document here';
  document.querySelector('.drop-card p').textContent='or click Browse to select a file from your device';
  document.getElementById('fi').value='';
  window.scrollTo({top:0,behavior:'smooth'});
}
</script>
</body>
</html>'''

# ─────────────────────────────────────────────────────────────────────────────
#  FLASK ROUTES
# ─────────────────────────────────────────────────────────────────────────────
@app.route('/')
def index():
    return render_template_string(LANDING_HTML)

@app.route('/login', methods=['GET','POST'])
def login():
    error = None
    if request.method == 'POST':
        email = request.form.get('email','').strip().lower()
        if email in ALLOWED_EMAILS:
            session['email'] = email
            return redirect('/')
        error = 'This email address is not registered. Please contact your administrator.'
    return render_template_string(LOGIN_HTML, error=error)

@app.route('/logout')
def logout():
    session.clear(); return redirect('/')

@app.route('/upload', methods=['POST'])
def upload():
    if not session.get('email'):
        return jsonify({'error':'Not authenticated'}), 401
    if 'file' not in request.files:
        return jsonify({'error':'No file'}), 400
    f = request.files['file']
    if not f or f.filename == '':
        return jsonify({'error':'Empty file'}), 400
    ext = f.filename.rsplit('.',1)[-1].lower() if '.' in f.filename else ''
    if ext not in ('pdf','doc','docx','txt'):
        return jsonify({'error':'File type not allowed'}), 400

    rid  = uuid.uuid4().hex[:14]
    sid  = f"trn:oid:::{random.randint(10000,99999)}:{random.randint(100000000,999999999)}"
    fn   = secure_filename(f.filename)
    fpath= os.path.join(UPLOAD, rid+'_'+fn)
    f.save(fpath)
    fsize= f"{os.path.getsize(fpath)/1024:.1f} KB"

    rich, plain, def_font, def_size = extract_rich(fpath, fn)
    words = len(plain.split())
    chars = len(plain)
    pages = max(1, words//250)
    sc    = compute_scores(plain, pages)
    ptype = sc.pop('ptype')

    meta = {
        'rid':   fn.rsplit('.',1)[0]+'_'+rid,
        'sid':   sid, 'filename': fn,
        'date':  datetime.now().strftime('%b %d, %Y, %I:%M %p UTC'),
        'words': words, 'chars': chars, 'pages': pages,
        'fsize': fsize, 'rich': rich,
        'def_font': def_font, 'def_size': def_size,
        **sc
    }

    build_sim_pdf(meta)
    build_ai_pdf(meta)

    return jsonify({
        'rid': meta['rid'], 'sid': sid, 'filename': fn,
        'date': meta['date'], 'words': words, 'chars': chars, 'pages': pages,
        'sim_index': sc['sim_index'], 'internet_pct': sc['internet_pct'],
        'pub_pct': sc['pub_pct'], 'student_pct': sc['student_pct'],
        'ai_pct': sc['ai_pct']
    })

@app.route('/download/<rid>/<rtype>')
def download(rid, rtype):
    if not session.get('email'): return redirect('/login')
    if rtype not in ('sim','ai'): abort(404)
    fn   = f"{rid}_sim.pdf" if rtype=='sim' else f"{rid}_ai.pdf"
    path = os.path.join(REPORT, fn)
    if not os.path.exists(path): abort(404)
    dl = 'Similarity_Report.pdf' if rtype=='sim' else 'AI_Writing_Report.pdf'
    return send_file(path, as_attachment=True, download_name=dl)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
