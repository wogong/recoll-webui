#{{{ imports
import os
import bottle
import time
import sys
import datetime
import glob
import hashlib
import csv
import io
import string
import shlex
from urllib.parse import quote as urlquote
from recoll import recoll, rclextract, rclconfig

def msg(s):
    print("%s" % s, file=sys.stderr)

# use ujson if avalaible (faster than built in json)
try:
    import ujson as json
except ImportError:
    import json
    #msg("ujson module not found, using (slower) built-in json module instead")

g_fscharset=sys.getfilesystemencoding()

#}}}
#{{{ settings
# settings defaults
DEFAULTS = {
    'context': 30,
    'stem': 1,
    'timefmt': '%c',
    'dirdepth': 2,
    'maxchars': 500,
    'maxresults': 0,
    'perpage': 25,
    'csvfields': 'filename title author size time mtype url',
    'title_link': 'download',
    'collapsedups': 0,
    'synonyms': "",
    'mounts': {
        # Override default links for directories.
        # Useful for rewriting links to access the files on a server.
        # If not specified, the url will be dir path prefixed with 'file://'.
        #
        # Path in recoll.conf   : Remote url
        # '/media/data/docs'    : 'https://media.server.com/docs',
        # '/var/www/data'       : 'file:///192.168.1.2/data',
    },
    "noresultlinks":  0,
    "logquery": 0
}

# sort fields/labels
SORTS = [
    ("relevancyrating", "Relevancy"),
    ("mtime", "Date",),
    ("url", "Path"),
    ("filename", "Filename"),
    ("fbytes", "Size"),
    ("author", "Author"),
]

# doc fields
FIELDS = [
    # exposed by python api
    'abstract',
    'author',
    'collapsecount',
    'dbytes',
    'dmtime',
    'fbytes',
    'filename',
    'fmtime',
    'ipath',
    'keywords',
    'mtime',
    'mtype',
    'origcharset',
    'relevancyrating',
    'sig',
    'size',
    'title',
    'url',
    # calculated
    'label',
    'snippet',
    'time',
]
#}}}
#{{{  functions
#{{{  helpers
def select(ls, invalid=[None]):
    for value in ls:
        if value not in invalid:
            return value

def timestr(secs, fmt):
    # Just in case: we had a bug at some point inserting commas in the dmtime field.
    secs = secs.strip(',')
    if secs == '' or secs is None:
        secs = '0'
    t = time.gmtime(int(secs))
    return time.strftime(fmt, t)

# Compute a file name used for an attachment 'filename' attribute. We don't know what the remote
# system would accept, so play it safe
_g_valid_filename_chars = "_-%s%s" % (string.ascii_letters, string.digits)
def normalise_filename(fn):
    out = ''.join(c if c in _g_valid_filename_chars else "_" for c in fn)
    return out

# We may need to get the "topdirs" value from other directories than our main one.
def get_topdirs(confdir):
    rclconf = rclconfig.RclConfig(confdir)
    return rclconf.getConfParam('topdirs')

# Environment fetch for the cases where we don't care if unset or null
def safe_envget(varnm):
    try:
        return os.environ[varnm]
    except Exception as ex:
        return None

# Get the database directory from recoll.conf, defaults to confdir/xapiandb. Note
# that this is available as getDbDir() from recoll 1.27 (2020)
def get_dbdir(confdir):
    confdir = os.path.expanduser(confdir)
    rclconf = rclconfig.RclConfig(confdir)
    try:
        dbdir = rclconf.getDbDir()
    except:
        dbdir = rclconf.getConfParam('dbdir')
        if not dbdir:
            dbdir = 'xapiandb'
        if not os.path.isabs(dbdir):
            cachedir = rclconf.getConfParam('cachedir')
            if not cachedir:
                cachedir = confdir
            dbdir = os.path.join(cachedir, dbdir)
    # recoll API expects bytes, not strings
    return os.path.normpath(dbdir).encode(g_fscharset)

#}}}
#{{{ get_config
def get_config():
    # Arrange for apache wsgi SetEnv values to be reflected in the os environment.
    # This allows people to use either method
    for k in ("RECOLL_CONFDIR", "RECOLL_EXTRACONFDIRS"):
        if  k in bottle.request.environ:
            os.environ[k] = bottle.request.environ[k]
    config = {}
    envdir = safe_envget('RECOLL_CONFDIR')
    # get useful things from recoll.conf
    rclconf = rclconfig.RclConfig(envdir)
    config['confdir'] = rclconf.getConfDir()
    config['dirs'] = dict.fromkeys([os.path.expanduser(d) for d in
                                    shlex.split(rclconf.getConfParam('topdirs'))],
                                   config['confdir'])
    # add topdirs from extra config dirs
    extraconfdirs = safe_envget('RECOLL_EXTRACONFDIRS')
    if extraconfdirs:
        config['extraconfdirs'] = shlex.split(extraconfdirs)
        for e in config['extraconfdirs']:
            config['dirs'].update(dict.fromkeys([os.path.expanduser(d) for d in
                shlex.split(get_topdirs(e))],e))
        config['extradbs'] = list(map(get_dbdir, config['extraconfdirs']))
    else:
        config['extraconfdirs'] = None
        config['extradbs'] = None
    config['stemlang'] = rclconf.getConfParam('indexstemminglanguages')

    # Possibly adjust user config defaults with data from recoll.conf. Some defaults which are
    # generally suitable like dirdepth=2 can be unworkable on big data sets (causing init errors so
    # that they can't even be adjusted from the UI). The 2nd parameter asks for an int conversion
    fetches = [("context", 1), ("stem", 1),("timefmt", 0),("dirdepth", 1),("maxchars", 1),
               ("maxresults", 1), ("perpage", 1), ("csvfields", 0), ("title_link", 0),
               ("collapsedups", 1), ("synonyms", 0), ("noresultlinks", 1), ("logquery", 1)]
    for k, isint in fetches:
        value = rclconf.getConfParam("webui_" + k)
        if value is not None:
            DEFAULTS[k] = int(value) if isint else value
    # get config from cookies or defaults
    for k, v in DEFAULTS.items():
        value = select([bottle.request.get_cookie(k), v], invalid=["None", None])
        config[k] = type(v)(value)
    # Fix csvfields: get rid of invalid ones to avoid needing tests in the dump function
    cf = config['csvfields'].split()
    ncf = [f for f in cf if f in FIELDS]
    config['csvfields'] = ' '.join(ncf)
    config['fields'] = ' '.join(FIELDS)
    # get mountpoints
    config['mounts'] = {}
    for d in config['dirs']:
        name = 'mount_%s' % urlquote(d,'')
        config['mounts'][d] = select([bottle.request.get_cookie(name),
                                      rclconf.getConfParam(f"webui_mount_{d}"),
                                      f"file://{d}"],
                                     [None, ''])

    # Parameters set by the admin in the recoll configuration
    # file. These override anything else, so read them last
    val = rclconf.getConfParam('webui_nojsoncsv')
    val = 0 if val is None else int(val)
    config['rclc_nojsoncsv'] = val

    val = rclconf.getConfParam('webui_maxperpage')
    val = 0 if val is None else int(val)
    if val:
        if config['perpage'] == 0 or config['perpage'] > val:
            config['perpage'] = val

    val = rclconf.getConfParam('webui_nosettings')
    val = 0 if val is None else int(val)
    config['rclc_nosettings'] = val

    val = str(rclconf.getConfParam('webui_defaultsort'))
    config['defsortidx'] = 0
    for i in range(len(SORTS)):
        if SORTS[i][0] == val or SORTS[i][1] == val:
            config['defsortidx'] = i
            break
    return config
#}}}
#{{{ get_dirs
def get_dirs(tops, depth):
    v = []
    for top in tops:
        # We do the conversion to bytes here, because Python versions
        # before 3.7 won't do the right thing if the locale is C,
        # which would be the case with a default apache install
        top = top.encode('utf-8', 'surrogateescape')
        dirs = [top]
        for d in range(1, depth+1):
            dirs = dirs + glob.glob(top + b'/*' * d)
        dirs = filter(lambda f: os.path.isdir(f), dirs)
        top_path = top.rsplit(b'/', 1)[0]
        dirs = [w.replace(top_path+b'/', b'', 1) for w in dirs]
        v = v + dirs
    for i in range(len(v)):
        v[i] = v[i].decode('utf-8', 'surrogateescape')
    return ['<all>'] + v
#}}}
#{{{ get_query
def get_query(config=None):
    defsortidx = config['defsortidx'] if config and 'defsortidx' in config else 0
    query = {
        'query': select([bottle.request.query.query, '']),
        'before': select([bottle.request.query.before, '']),
        'after': select([bottle.request.query.after, '']),
        'dir': select([bottle.request.query.dir, '', '<all>'], [None, '']),
        'sort': select([bottle.request.query.sort, SORTS[defsortidx][0]], [None, '']),
        'ascending': int(select([bottle.request.query.ascending, 0], [None, ''])),
        'page': int(select([bottle.request.query.page, 0], [None, ''])),
        'highlight': int(select([bottle.request.query.highlight, 1], [None, ''])),
        'snippets': int(select([bottle.request.query.snippets, 1], [None, ''])),
    }
    #msg("query['query'] : %s" % query['query'])
    return query
#}}}
#{{{ query_to_recoll_string
def query_to_recoll_string(q):
    qs = q['query']
    if len(q['after']) > 0 or len(q['before']) > 0:
        qs += " date:%s/%s" % (q['after'], q['before'])
    qdir = q['dir']
    if qdir != '<all>':
        qs += " dir:\"%s\" " % qdir
    return qs
#}}}
#{{{ recoll_initsearch
def recoll_initsearch(q):
    config = get_config()
    confdir = config['confdir']
    dbs = []
    """ The reason for this somewhat elaborate scheme is to keep the
    set size as small as possible by searching only those databases
    with matching topdirs """
    if q['dir'] == '<all>':
        if config['extraconfdirs']:
            dbs.extend(map(get_dbdir,config['extraconfdirs']))
    else:
        confdirs = []
        for d,conf in config['dirs'].items():
            tdbasename = os.path.basename(d)
            if os.path.commonprefix([tdbasename, q['dir']]) == tdbasename:
                confdirs.append(conf)
        if len(confdirs) == 0:
            # should not happen, using non-existing q['dir']?
            bottle.abort(400, 'no matching database for search directory ' + q['dir'])
        elif len(confdirs) == 1:
            # only one config (most common situation)
            confdir = confdirs[0]
        else:
            # more than one config with matching topdir, use 'm all
            confdir = confdirs[0]
            dbs.extend(map(get_dbdir, confdirs[1:]))

    if config['extradbs']:
        dbs.extend(config['extradbs'])

    db = recoll.connect(confdir, extra_dbs=dbs)

    # Compare to "None" because of the conv. to str done while setting from cookies
    if config["synonyms"] and config["synonyms"] != "None":
        try:
            db.setSynonymsFile(config["synonyms"])
        except:
            # Only supported from recoll 1.40.3, just ignore the error for now
            msg(f"Setting synonyms to [{config['synonyms']}] failed")
            pass

    db.setAbstractParams(config['maxchars'], config['context'])
    query = db.query()
    query.sortby(q['sort'], q['ascending'])
    try:
        qs = query_to_recoll_string(q)
        if "logquery" in config and config["logquery"]:
            msg(f"Query: {qs}")
        query.execute(qs, config['stem'], config['stemlang'],
                      collapseduplicates=config['collapsedups'])
    except Exception as ex:
        msg("Query execute failed: %s" % ex)
        pass
    return query
#}}}
#{{{ HlMeths
class HlMeths:
    def startMatch(self, idx):
        return '<span class="search-result-highlight">'
    def endMatch(self):
        return '</span>'
#}}}
#{{{ recoll_search
def recoll_search(q):
    config = get_config()
    tstart = datetime.datetime.now()
    results = []
    query = recoll_initsearch(q)
    nres = query.rowcount

    if config['maxresults'] == 0:
        config['maxresults'] = nres
    if nres > config['maxresults']:
        nres = config['maxresults']
    if config['perpage'] == 0 or q['page'] == 0:
        config['perpage'] = nres
        q['page'] = 1
    offset = (q['page'] - 1) * config['perpage']

    if query.rowcount > 0:
        if type(query.next) == int:
            query.next = offset
        else:
            query.scroll(offset, mode='absolute')

    if 'highlight' in q and q['highlight']:
        highlighter = HlMeths()
    else:
        highlighter = None
    for i in range(config['perpage']):
        try:
            doc = query.fetchone()
            # Later Recoll versions return None at EOL instead of
            # exception This change restores conformance to PEP 249
            # Python Database API Specification
            if not doc:
                break
        except:
            break
        d = {}
        for f in FIELDS:
            v = getattr(doc, f)
            if v is not None:
                d[f] = v
            else:
                d[f] = ''
        d['label'] = select([d['title'], os.path.basename(d['url']), '?'], [None, ''])
        d['sha'] = hashlib.sha1((d['url']+d['ipath']).encode('utf-8')).hexdigest()
        d['time'] = timestr(d['mtime'], config['timefmt'])
        if 'snippets' in q and q['snippets']:
            if highlighter:
                d['snippet'] = query.makedocabstract(doc, methods=highlighter)
            else:
                d['snippet'] = query.makedocabstract(doc)
            if not d['snippet']:
                try:
                    d['snippet'] = doc['abstract']
                except:
                    pass
        #for n,v in d.items():
        #    print("type(%s) is %s" % (n,type(v)))
        results.append(d)
    tend = datetime.datetime.now()
    return results, nres, tend - tstart
#}}}
#}}}
#{{{ routes
#{{{ static
@bottle.route('/static/:path#.+#')
def server_static(path):
    return bottle.static_file(path, root='./static')
#}}}
#{{{ main
@bottle.route('/')
@bottle.view('main')
def main():
    config = get_config()
    return { 'dirs': get_dirs(config['dirs'], config['dirdepth']),
            'query': get_query(config), 'sorts': SORTS, 'config': config}
#}}}
#{{{ results
@bottle.route('/results')
@bottle.view('results')
def results():
    config = get_config()
    query = get_query(config)
    qs = query_to_recoll_string(query)
    res, nres, timer = recoll_search(query)
    if config['maxresults'] == 0:
        config['maxresults'] = nres
    if config['perpage'] == 0:
        config['perpage'] = nres
    return { 'res': res, 'time': timer, 'query': query, 'dirs':
             get_dirs(config['dirs'], config['dirdepth']),
             'qs': qs, 'sorts': SORTS, 'config': config,
             'query_string': bottle.request.query_string, 'nres': nres,
             'config': config}
#}}}
#{{{ preview
@bottle.route('/preview/<resnum:int>')
def preview(resnum):
    config = get_config()
    query = get_query(config)
    qs = query_to_recoll_string(query)
    rclq = recoll_initsearch(query)
    if resnum > rclq.rowcount - 1:
        return 'Bad result index %d' % resnum
    rclq.scroll(resnum)
    doc = rclq.fetchone()
    xt = rclextract.Extractor(doc)
    tdoc = xt.textextract(doc.ipath)
    if tdoc.mimetype == 'text/html':
        ishtml = 1
        bottle.response.content_type = 'text/html; charset=utf-8'
    else:
        ishtml = 0
        bottle.response.content_type = 'text/plain; charset=utf-8'
    if 'highlight' in query and query['highlight']:
        hl = HlMeths()
        txt = rclq.highlight(tdoc.text, ishtml=ishtml, methods=hl)
        pos = txt.find('<head>')
        ssref = '<link rel="stylesheet" type="text/css" href="../static/style.css">'
        if pos >= 0:
            txt = txt[0:pos+6] + ssref + txt[pos+6:]
        else:
            txt = '<html><head>' + ssref + \
                '<meta http-equiv="Content-Type" content="text/html; charset=UTF-8"></head><body>'+ \
                txt
        bottle.response.content_type = 'text/html; charset=utf-8'
        return txt
    return tdoc.text
#}}}
#{{{ download
@bottle.route('/download/<resnum:int>')
def edit(resnum):
    config = get_config()
    query = get_query(config)
    qs = query_to_recoll_string(query)
    rclq = recoll_initsearch(query)
    if resnum > rclq.rowcount - 1:
        return 'Bad result index %d' % resnum
    rclq.scroll(resnum)
    doc = rclq.fetchone()
    bottle.response.content_type = doc.mimetype
    xt = rclextract.Extractor(doc)
    path = xt.idoctofile(doc.ipath, doc.mimetype)
    if "filename" in doc.keys():
        filename = doc.filename
    else:
        filename = os.path.basename(path)
    bottle.response.headers['Content-Disposition'] = f'attachment; filename="{filename}"'
    bottle.response.headers['Content-Length'] = os.stat(path).st_size
    f = open(path, 'rb')
    try:
        os.unlink(path)
    except:
        pass
    return f
#}}}
#{{{ json
@bottle.route('/json')
def get_json():
    config = get_config()
    query = get_query(config)
    qs = query_to_recoll_string(query)
    bottle.response.headers['Content-Type'] = 'application/json'
    bottle.response.headers['Content-Disposition'] = \
      'attachment; filename=recoll-%s.json' % normalise_filename(qs)
    res, nres, timer = recoll_search(query)
    ures = []
    for d in res:
        ud={}
        for f,v in d.items():
            ud[f] = v
        ures.append(ud)
    res = ures
    return json.dumps({ 'query': query, 'results': res })
#}}}
#{{{ csv
@bottle.route('/csv')
def get_csv():
    config = get_config()
    query = get_query(config)
    query['page'] = 0
    query['snippets'] = 0
    qs = query_to_recoll_string(query)
    bottle.response.headers['Content-Type'] = 'text/csv'
    bottle.response.headers['Content-Disposition'] = \
      'attachment; filename=recoll-%s.csv' % normalise_filename(qs)
    res, nres, timer = recoll_search(query)
    si = io.StringIO()
    cw = csv.writer(si)
    fields = config['csvfields'].split()
    cw.writerow(fields)
    for doc in res:
        row = []
        for f in fields:
            if f in doc:
                row.append(doc[f])
            else:
                row.append('')
        cw.writerow(row)
    return si.getvalue().strip("\r\n")
#}}}
#{{{ settings/set
@bottle.route('/settings')
@bottle.view('settings')
def settings():
    return get_config()

@bottle.route('/set')
def set():
    config = get_config()
    for k, v in DEFAULTS.items():
        bottle.response.set_cookie(k, str(bottle.request.query.get(k)),
                                   max_age=3153600000, expires=315360000)
    for d in config['dirs']:
        # We should not set the cookie if the value is the default (identical path). This would
        # allow the server configuration setting to be used if set. This would also show a wrong
        # value in the settings screen (default instead of server config value), so not too sure
        # what the right thing would be here.
        cookie_name = 'mount_%s' % urlquote(d, '')
        bottle.response.set_cookie(cookie_name, str(bottle.request.query.get('mount_%s' % d)),
                                   max_age=3153600000, expires=315360000)
    bottle.redirect('./')
#}}}
#{{{ osd
@bottle.route('/osd.xml')
@bottle.view('osd')
def main():
    #config = get_config()
    url = bottle.request.urlparts
    url = '%s://%s' % (url.scheme, url.netloc)
    return {'url': url}
#}}}
# vim: fdm=marker:tw=80:ts=4:sw=4:sts=4:et
