import os
import base64
import urllib2
import urllib
import urlparse
import fnmatch
import zipfile
import mapnik
import shutil
import cascadenik
import tornado.httpclient
import tempfile
import time
import copy
import logging

"""

Cache backend for TileLive. Includes a MapCache backend for mapfiles,
and a DataCache backend for data files. Static cache of 10 each, plus 
non-managed file cache of all files of each.

"""

try:
    import lxml.etree as ElementTree
    from lxml.etree import Element
except ImportError:
    try:
        import xml.etree.ElementTree as ElementTree
        from xml.etree.ElementTree import Element
    except ImportError:
        import elementtree.ElementTree as ElementTree
        from elementtree.ElementTree import Element
 
def locate(pattern, root=os.curdir):
   """ find a file in a directory and its subdirectories """
   for path, dirs, files in os.walk(os.path.abspath(root)):
       for filename in fnmatch.filter(files, pattern):
           yield os.path.join(path, filename)

class TLCache(object):
    """ base cache object for TileLite """
    def __init__(self, **kwargs):
        self.directory = kwargs.get('directory', '')
        if not os.path.isdir(self.directory): os.mkdir(self.directory)

    def url2fs(self, url):
        """ encode a URL to be safe as a filename """
        uri, extension = os.path.splitext(url)
        return base64.urlsafe_b64encode(uri) + extension

    def fs2url(self, url):
        """ decode a filename to the URL it is derived from """
        return base64.urlsafe_b64decode(url)

    def filecache(self, in_url):
        """ given a URL, return a local file path """
        local_url = os.path.join(self.directory, in_url)
        if not os.path.isfile(local_url):
            url = self.fs2url(in_url)
            remote_file = urllib2.urlopen(url)
            output = open(local_url, 'wb')
            output.write(remote_file.read())
            output.close()
        return local_url

"""
PreCache handler for TL. Provides an asynchronous queue of shapefile requests
corresponding to a given map. Once all shapefile requests have been made and
unzipped, the callback function at PreCache.execute(callback) is called. A
shared locking mechanism can be passed such that concurrent requests do not
simultaneously download the same remote resources.
"""
class PreCache(TLCache):
    def __init__(self, **kwargs):
        if kwargs.has_key('locks'):
            self.locks = kwargs['locks']
        else:
            self.locks = []
        self.directory = kwargs['directory']
        self.request_handler = kwargs['request_handler']
        self.queue = []
        self.callback = None
        self.kwargs = None
        if not os.path.isdir(self.directory): os.mkdir(self.directory)

    """ add a request to the queue """
    def add(self, url):
        self.queue.append(url)

    """ execute all requests and fire callback once completed """
    def execute(self, callback, **kwargs):
        self.callback = callback
        self.kwargs = kwargs
        for url in copy.copy(self.queue):
            self.process_request(url)
        if len(self.queue) == 0 and len(self.locks) == 0:
            self.callback(**self.kwargs)

    def process_request(self, request_url):
        # Directory exists, request has already been successfully processed.
        base_dir = os.path.join(self.directory, base64.urlsafe_b64encode(request_url))
        if os.path.isdir(base_dir):
            if request_url in self.queue : self.queue.remove(request_url)
            if request_url in self.locks : self.locks.remove(request_url)
        # Request is in queue and not locked. Fire asynchronous HTTP request.
        elif request_url in self.queue and request_url not in self.locks:
            self.queue.remove(request_url)
            self.locks.append(request_url)
            http = tornado.httpclient.AsyncHTTPClient()
            http.fetch(request_url, callback=self.cache)
        # Request is in locks. Perform a holding pattern.
        elif request_url in self.locks:
            logging.info("Locked: %s", request_url)
            tornado.ioloop.IOLoop.instance().add_timeout(time.time() + 5, lambda: self.process_request(request_url))
        # All queued requests have been processed. Continue to callback.
        if len(self.queue) == 0 and len(self.locks) == 0:
            self.callback(**self.kwargs)
        return

    """ asynchttp request callback. caches the downloaded zipfile. """
    def cache(self, response):
        import StringIO, zipfile
        try:
            zip_file = zipfile.ZipFile(StringIO.StringIO(response.body))
            infos = zip_file.infolist()
            extensions = [os.path.splitext(info.filename)[1].lower() for info in infos]
            basenames = [os.path.basename(info.filename).lower() for info in infos]
            base_dir = os.path.join(self.directory, base64.urlsafe_b64encode(response.effective_url))
            # Caching only requires that .shp is present
            for (expected, required) in (('.shp', True), ('.shx', False), ('.dbf', False), ('.prj', False)):
                if required and expected not in extensions:
                    raise Exception('Zip file %(shapefile)s missing extension "%(expected)s"' % locals())
                for (info, extension, basename) in zip(infos, extensions, basenames):
                    if extension == expected:
                        if not os.path.isdir(base_dir):
                            os.mkdir(base_dir)
                        file_data = zip_file.read(info.filename)
                        file_name = os.path.normpath('%(base_dir)s/%(basename)s' % locals())
                        file = open(file_name, 'wb')
                        file.write(file_data)
                        file.close()
        except:
            logging.info('Failed: %s', response.effective_url)
            self.locks.remove(response.effective_url)
            self.queue.append(response.effective_url)
            self.request_handler.finish()
            return
        self.locks.remove(response.effective_url)
        if len(self.queue) == 0 and len(self.locks) == 0:
            self.callback(**self.kwargs)

class MapCache(TLCache):
    """ mapfile and mapnik map cache """
    def __init__(self, **kwargs):
        self.directory = kwargs['directory']
        self.mapnik_maps = {}
        self.mapnik_locks = {}
        self.size = kwargs.get('size', 10)
        self.tilesize = kwargs.get('tilesize', 256)
        if not os.path.isdir(self.directory): os.mkdir(self.directory)

    def compile(self, url, compile_callback):
        self.mapnik_maps[url] = mapnik.Map(self.tilesize, self.tilesize)
        open("%s_compiled.xml" % self.filecache(url), 'w').write(cascadenik.compile(self.filecache(url), urlcache=True))
        mapnik.load_map(self.mapnik_maps[url], "%s_compiled.xml" % self.filecache(url))
        compile_callback(self.mapnik_maps[url])

    def get(self, url, request_handler, callback):
        """ get a mapnik.Map object from a URL of a map.xml file, 
        regardless of cache status """
        if not self.mapnik_maps.has_key(url):
            if not self.mapnik_locks.has_key(url):
                self.mapnik_locks[url] = []
            precache = PreCache(directory=tempfile.gettempdir(), request_handler=request_handler, locks=self.mapnik_locks[url])
            doc = ElementTree.parse(urllib.urlopen(self.filecache(url)))
            map = doc.getroot()
            for layer in map.findall('Layer'):
                for parameter in layer.find('Datasource').findall('Parameter'):
                    if parameter.get('name', None) == 'file':
                        (scheme, netloc, path, params, query, fragment) = urlparse.urlparse(parameter.text)
                        if scheme != '':
                            precache.add(parameter.text)
            precache.execute(self.compile, url=url, compile_callback=callback)
        else:
            callback(self.mapnik_maps[url])

    def remove(self, url):
        print url
        """ remove a map file, object and associated tiles from the cache """
        try:
            # remove the object and data files
            if self.mapnik_maps.has_key(url):
                del self.mapnik_maps[url]
            if self.mapnik_locks.has_key(url):
                del self.mapnik_locks[url]
            if os.path.isdir(os.path.join(self.directory, url)):
                shutil.rmtree(os.path.join(self.directory, url))

        except Exception, e:
            return False

    def list(self):
        """ return a list of cached URLs """
        return map(self.fs2url, 
              [x for x in os.listdir(self.directory) if 
                os.path.isfile(os.path.join(self.directory, x))]
          )
