'''
Created on 3.10.2012

@author: marko
'''
import os
import socket
import sys
from twisted.internet import defer
from xml.etree.cElementTree import ElementTree

from Components.config import config, ConfigSelection
from Plugins.Extensions.archivCZSK import _, log, settings, version as aczsk
from Plugins.Extensions.archivCZSK.settings import VIDEO_EXTENSIONS, SUBTITLES_EXTENSIONS
from Plugins.Extensions.archivCZSK.engine.exceptions.addon import AddonError
from Plugins.Extensions.archivCZSK.engine.player.player import Player 
from Plugins.Extensions.archivCZSK.resources.repositories import repo_modules
from downloader import DownloadManager
from items import PVideo, PFolder, PPlaylist, PDownload, PCategory, PVideoAddon, \
    PCategoryVideoAddon, PUserCategory, Stream, RtmpStream
from serialize import CategoriesIO, FavoritesIO
from tools import task, util
PNG_PATH = settings.IMAGE_PATH

CREATE_DEFAULT_HTTPS_CONTEXT = None
try:
    import ssl
    CREATE_DEFAULT_HTTPS_CONTEXT = ssl._create_default_https_context
except:
    pass


class SysPath(list):
    """to append sys path only to addon which belongs to"""
    def __init__(self, addons):
        self.addons = addons
    def append(self, val):
        print '[AddonSysPath] append', val
        for addon in self.addons:
            if val.find(addon.id) != -1:
                addon.loader.add_path(val)

class CustomSysImporter(util.CustomImporter):
    def __init__(self, custom_sys):
        util.CustomImporter.__init__(self, 'custom_sys',  log=log.debug)
        self.add_module('sys', custom_sys)

class AddonSys():
    "sys for addons"
    def __init__(self):
        self.addons = []
        self.path = SysPath(self.addons)

    def __setitem__(self, key, val):
        if key == 'path':
            print 'you cannot replace AddonSysPath'
        else:
            dict.__setitem__(self, key, val)

    def __getattr__(self, attr):
        if attr=='path':
            return self.path
        return getattr(sys, attr)

    def add_addon(self, addon):
        self.addons.append(addon)

    def remove_addon(self, addon):
        self.addons.remove(addon)

    def clear_addons(self):
        del self.addons[:]


class ContentProvider(object):
    """ Provides item content which can be shown in GUI
       All item content which can be created is in items module
    """

    def __init__(self):
        self.capabilities = []
        self.on_start = []
        self.on_stop = []
        self.on_pause = []
        self.on_resume = []
        self.__started = False
        self.__paused = False

    def __str__(self):
        return "%s"% self.__class__.__name__

    def is_seekable(self):
        return True

    def is_pausable(self):
        return True

    def get_capabilities(self):
        return self.capabilities

    def get_content(self, params={}):
        """get content with help of params
          @return: should return list of items created in items module"""
        pass

    def start(self):
        if self.__started:
            log.info("[%s] cannot start, provider is already started",self)
            return
        self.__started = True
        self.__paused = False
        for f in self.on_start:
            f()
        log.info("[%s] started", self)

    def stop(self):
        if not self.__started:
            log.info("[%s] cannot stop, provider is already stopped",self)
            return
        self.__started = False
        self.__paused = False
        for f in self.on_stop:
            f()
        log.info("[%s] stopped", self)

    def resume(self):
        if not self.__started:
            log.info("[%s] cannot resume, provider not started yet",self)
            return
        if not self.__paused:
            log.info("[%s] cannot resume, provider is already running",self)
            return
        self.__paused = False
        for f in self.on_resume:
            f()
        log.info("[%s] resumed", self)

    def pause(self):
        if not self.__started:
            log.info("[%s] cannot pause, provider not started yet",self)
            return
        if self.__paused:
            log.info("[%s] cannot pause, provider is already paused",self)
            return
        self.__paused = True
        for f in self.on_pause:
            f()
        log.info("[%s] paused", self)


class Media(object):
    def __init__(self, player_cls, allowed_download=True):
        self.player = None
        self.player_cls = player_cls
        self.player_cfg = config.plugins.archivCZSK.videoPlayer
        self.capabilities.append('play')
        if allowed_download:
            self.capabilities.append('play_and_download')
            #self.capabilities.append('play_and_download_gst')
        self.on_stop.append(self.__delete_player)

    def __delete_player(self):
        self.player = None

    def play(self, session, item, mode, cb=None):
        if not self.player:
            use_video_controller = self.player_cfg.useVideoController.value
            self.player = self.player_cls(session, cb, use_video_controller)
        seekable = self.is_seekable()
        pausable = self.is_pausable()
        self.player.setMediaItem(item, seekable=seekable, pausable=pausable)
        self.player.setContentProvider(self)
        if mode in self.capabilities:
            if mode == 'play':
                self.player.play()
            elif mode == 'play_and_download':
                self.player.playAndDownload()
            elif mode == 'play_and_download_gst':
                self.player.playAndDownload(True)
        else:
            log.info('Invalid playing mode - %s', str(mode))


class Favorites(object):
    def __init__(self, shortcuts_path):
        self.shortcuts = FavoritesIO(os.path.join(shortcuts_path, 'shortcuts'))
        self.capabilities.append('favorites')
        self.on_stop.append(self.save_shortcuts)

    def create_shortcut(self, favorite):
        return self.shortcuts.add_favorite(favorite)

    def remove_shortcut(self, favorite):
        return self.shortcuts.remove_favorite(favorite)

    def get_shortcuts(self):
        return self.shortcuts.get_favorites()

    def save_shortcuts(self):
        self.shortcuts.save()


class Downloads(object):
    def __init__(self, downloads_path, allowed_download):
        self.downloads_path = downloads_path
        if allowed_download:
            self.capabilities.append('download')

    def get_downloads(self):
        video_lst = []
        if not os.path.isdir(self.downloads_path):
            util.make_path(self.downloads_path)

        downloads = os.listdir(self.downloads_path)
        for download in downloads:
            download_path = os.path.join(self.downloads_path, download)

            if os.path.isdir(download_path):
                continue

            if os.path.splitext(download_path)[1] in VIDEO_EXTENSIONS:
                filename = os.path.basename(os.path.splitext(download_path)[0])
                url = download_path
                subs = None
                if filename in [os.path.splitext(x)[0] for x in downloads if os.path.splitext(x)[1] in SUBTITLES_EXTENSIONS]:
                    subs = filename + ".srt"

                it = PDownload(download_path)
                it.name = filename
                it.url = url
                it.subs = subs

                downloadManager = DownloadManager.getInstance()
                download = downloadManager.findDownloadByIT(it)

                if download is not None:
                    it.finish_time = download.finish_time
                    it.start_time = download.start_time
                    it.state = download.state
                    it.textState = download.textState
                video_lst.append(it)

        return video_lst


    def download(self, item, startCB, finishCB, playDownload=False, mode="", overrideCB=None):
        """Downloads item PVideo itemem calls startCB when download starts
           and finishCB when download finishes
        """
        quiet = False
        headers = item.settings['extra-headers']
        log.debug("Download headers %s", headers)
        downloadManager = DownloadManager.getInstance()
        d = downloadManager.createDownload(name=item.name, url=item.url, stream=item.stream, filename=item.filename,
                                           live=item.live, destination=self.downloads_path,
                                           startCB=startCB, finishCB=finishCB, quiet=quiet,
                                           playDownload=playDownload, headers=headers, mode=mode)
        if item.subs is not None and item.subs != '':
            log.debug('subtitles link: %s' , item.subs)
            subs_file_path = os.path.splitext(d.local)[0] + '.srt'
            util.download_to_file(item.subs, subs_file_path)
        downloadManager.addDownload(d, overrideCB)

    def remove_download(self, item):
        if item is not None and isinstance(item, PDownload):
            log.debug('removing item %s from disk' % item.name)
            os.remove(item.path.encode('utf-8'))
        else:
            log.info('cannot remove item %s from disk, not PDownload instance', str(item))


class ArchivCZSKContentProvider(ContentProvider):
    def __init__(self, archivczsk, path):
        ContentProvider.__init__(self)
        self._archivczsk = archivczsk
        self._categories_io = CategoriesIO(path)
        self.on_start.append(self.__create_config)
        self.on_pause.append(self.__create_config)
        self.on_stop.append(self.__save_categories)

        all_addons_category = PCategory()
        all_addons_category.name = _("All addons")
        all_addons_category.params = {'category_addons':'all_addons'}
        all_addons_category.image = PNG_PATH + '/category_all.png'
        tv_addons_category = PCategory()
        tv_addons_category.name = _("TV addons")
        tv_addons_category.image = PNG_PATH+'/category_tv.png'
        tv_addons_category.params = {'category_addons':'tv_addons'}
        video_addons_category = PCategory()
        video_addons_category.name = _("Video addons")
        video_addons_category.image = PNG_PATH+'/category_video.png'
        video_addons_category.params = {'category_addons':'video_addons'}
        self.default_categories={
                                 'all_addons':{'item':all_addons_category
                                               ,'title':_("All addons"),
                                               'call':self._get_all_addons
                                               },
                                 'tv_addons':{
                                              'item':tv_addons_category,
                                              'title':_("TV addons"),
                                              'call':self._get_tv_addons
                                              },
                                  'video_addons':{
                                                  'item':video_addons_category,
                                                  'title':_("Video addons"),
                                                  'call':self._get_video_addons
                                                  }
                                  }
        self.default_categories_order = ['all_addons','tv_addons','video_addons']

    def __create_config(self):
        choicelist = [('categories', _("Category list"))]
        choicelist.extend([(category_key,self.default_categories[category_key]['title']) for category_key in self.default_categories_order])
        choicelist.extend([(category.id, category.name) for category in self._get_categories(user_only=True)])
        config.plugins.archivCZSK.defaultCategory = ConfigSelection(default='categories', choices=choicelist)

    def __save_categories(self):
        self._categories_io.save()

    def get_content(self, params=None):
        if not params or 'categories' in params:
            return self._get_categories()
        if 'category' in params:
            return self._get_category(params['category'])
        if 'category_addons' in params:
            if 'filter_enabled' in params:
                return self._get_category_addons(params['category_addons'], params['filter_enabled'])
            return self._get_category_addons(params['category_addons'])
        if 'categories_user' in params:
            return self._get_categories(user_only=True)

    def add_category(self, category_title):
        pcategory = PCategory()
        pcategory.name = category_title
        self._categories_io.add_category(pcategory)
        # update params
        pcategory.params = {'category_addons':pcategory.id}
        self._categories_io.update_category(pcategory)

    def rename_category(self, pcategory, new_title):
        # sync category
        pcategory = self._get_category(pcategory.id)
        pcategory.name = new_title
        self._categories_io.update_category(pcategory)
        # update params
        pcategory.params = {'category_addons':pcategory.id}
        self._categories_io.update_category(pcategory)

    def remove_category(self, pcategory):
        self._categories_io.remove_category(pcategory)

    def add_to_category(self, pcategory, paddon):
        # sync category
        pcategory = self._get_category(pcategory.id)
        pcategory.add_addon(paddon)
        self._categories_io.update_category(pcategory)

    def remove_from_category(self, pcategory, paddon):
        # sync category
        pcategory = self._get_category(pcategory.id)
        pcategory.remove_addon(paddon)
        self._categories_io.update_category(pcategory)

    def _get_category(self, category_id):
        if category_id in self.default_categories:
            return self.default_categories[category_id]['item']
        pcategory = self._categories_io.get_category(category_id)
        return pcategory

    def _get_categories(self, user_only=False):
        category_list = self._categories_io.get_categories()
        if not user_only:
            category_list.extend( [self.default_categories[category_key]['item'] for category_key in self.default_categories_order])
        return category_list

    def _get_category_addons(self, category_id, filter_enabled=True):
        def filter_enabled_addons(paddon):
            return paddon.addon.get_setting('enabled')
        if category_id in self.default_categories:
            return self.default_categories[category_id]['call'](filter_enabled)
        addons = [PCategoryVideoAddon(self._archivczsk.get_addon(addon_id)) for addon_id in self._categories_io.get_category(category_id)]
        if filter_enabled:
            addons = filter(filter_enabled_addons, addons)
        return addons

    def _get_all_addons(self, filter_enabled=True):
        def filter_enabled_addons(paddon):
            return paddon.addon.get_setting('enabled')
        addons = [PVideoAddon(addon) for addon in self._archivczsk.get_video_addons()]
        if filter_enabled:
            addons = filter(filter_enabled_addons, addons)
        return addons

    def _get_video_addons(self, filter_enabled=True):
        addons = [paddon for paddon in self._get_all_addons(filter_enabled) if not paddon.addon.get_setting('tv_addon')]
        addons.sort(key=lambda addon: addon.name.lower())
        return addons

    def _get_tv_addons(self, filter_enabled=True):
        addons = [paddon for paddon in self._get_all_addons(filter_enabled) if paddon.addon.get_setting('tv_addon')]
        addons.sort(key=lambda addon: addon.name.lower())
        return addons


class VideoAddonContentProvider(ContentProvider, Media, Downloads, Favorites):

    __resolving_provider = None
    __gui_item_list = [[], None, {}] #[0] for items, [1] for command to GUI [2] arguments for command
    __addon_sys = AddonSys()

    @classmethod
    def get_shared_itemlist(cls):
        return cls.__gui_item_list

    @classmethod
    def get_resolving_provider(cls):
        return cls.__resolving_provider

    @classmethod
    def get_resolving_addon(cls):
        return cls.__resolving_provider.video_addon

    def __init__(self, video_addon, downloads_path, shortcuts_path):
        allowed_download = not video_addon.get_setting('!download')
        self.video_addon = video_addon
        ContentProvider.__init__(self)
        Media.__init__(self, Player, allowed_download)
        Downloads.__init__(self,downloads_path, allowed_download)
        Favorites.__init__(self,shortcuts_path)
        self._dependencies = []
        self._sys_importer = CustomSysImporter(self.__addon_sys)
        self.on_start.append(self.__clean_sys_modules)
        self.on_start.append(self.__set_resolving_provider)
        self.on_stop.append(self.__unset_resolving_provider)
        self.on_stop.append(self.__restore_sys_modules)
        self.on_pause.append(self.__pause_resolving_provider)
        self.on_resume.append(self.__resume_resolving_provider)

    def __clean_sys_modules(self):
        self.saved_modules = {}
        for mod_name in repo_modules:
            if mod_name in sys.modules:
                mod = sys.modules[mod_name]
                del sys.modules[mod_name]
                self.saved_modules[mod_name] = mod
        del sys.modules['sys']

    def __restore_sys_modules(self):
        sys.modules['sys'] = sys
        sys.modules.update(self.saved_modules)
        del self.saved_modules

    def __set_resolving_provider(self):
        VideoAddonContentProvider.__resolving_provider = self
        VideoAddonContentProvider.__addon_sys.add_addon(self.video_addon)
        self.video_addon.include()
        self.resolve_dependencies()
        self.include_dependencies()
        sys.meta_path.append(self._sys_importer)

    def __unset_resolving_provider(self):
        VideoAddonContentProvider.__resolving_provider = None
        VideoAddonContentProvider.__addon_sys.clear_addons()
        self.video_addon.deinclude()
        self.release_dependencies()
        sys.meta_path.remove(self._sys_importer)

    def __pause_resolving_provider(self):
        self.video_addon.deinclude()
        for addon in self._dependencies:
            addon.deinclude()
        sys.meta_path.remove(self._sys_importer)

    def __resume_resolving_provider(self):
        self.video_addon.include()
        for addon in self._dependencies:
            addon.include()
        sys.meta_path.append(self._sys_importer)

    def __clear_list(self):
        del VideoAddonContentProvider.__gui_item_list[0][:]
        VideoAddonContentProvider.__gui_item_list[1] = None
        VideoAddonContentProvider.__gui_item_list[2].clear()

    def resolve_dependencies(self):
        from Plugins.Extensions.archivCZSK.archivczsk import ArchivCZSK
        log.info("trying to resolve dependencies for %s" , self.video_addon)
        for dependency in self.video_addon.requires:
            addon_id, version, optional = dependency['addon'], dependency['version'], dependency['optional']

            # checking if archivCZSK version is compatible with this plugin
            if addon_id == 'enigma2.archivczsk':
                if  not util.check_version(aczsk.version, version):
                    log.debug("archivCZSK version %s>=%s" , aczsk.version, version)
                else:
                    log.debug("archivCZSK version %s<=%s" , aczsk.version, version)
                    raise AddonError(_("You need to update archivCZSK at least to") + " " + version + " " + _("version"))

            log.info("%s requires %s addon, version %s" , self.video_addon, addon_id, version)
            if ArchivCZSK.has_addon(addon_id):
                tools_addon = ArchivCZSK.get_addon(addon_id)
                log.info("required %s founded" , tools_addon)
                if  not util.check_version(tools_addon.version, version):
                    log.debug("version %s>=%s" , tools_addon.version, version)
                    self._dependencies.append(tools_addon)
                else:
                    log.debug("version %s<=%s" , tools_addon.version, version)
                    if not optional:
                        log.error("cannot execute %s " , self.video_addon)
                        raise AddonError("Cannot execute addon %s, dependency %s version %s needs to be at least version %s"
                                        % (self.video_addon, tools_addon.id, tools_addon.version, version))
                    else:
                        log.debug("skipping")
                        continue
            else:
                log.info("required %s addon not founded" , addon_id)
                if not optional:
                    log.info("cannot execute %s addon" , self.video_addon)
                    raise Exception("Cannot execute %s, missing dependency %s" % (self.video_addon, addon_id))
                else:
                    log.debug("skipping")

    def include_dependencies(self):
        for addon in self._dependencies:
            addon.include()
            self.__addon_sys.add_addon(addon)

    def release_dependencies(self):
        log.debug("trying to release dependencies for %s" , self.video_addon)
        for addon in self._dependencies:
            addon.deinclude()
        del self._dependencies[:]

    def get_content(self, session, params, successCB, errorCB):
        log.debug('get_content - params:%s' % str(params))
        self.__clear_list()
        self.content_deferred = defer.Deferred()
        self.content_deferred.addCallbacks(successCB, errorCB)
        # setting timeout for resolving content
        loading_timeout = int(self.video_addon.get_setting('loading_timeout'))
        if loading_timeout > 0:
            socket.setdefaulttimeout(loading_timeout)

        try:
            ssl._create_default_https_context = ssl._create_unverified_context
        except:
            pass
        thread_task = task.Task(self._get_content_cb, self.run_script, session, params)
        thread_task.run()
        return self.content_deferred

    def run_script(self, session, params):
        script_path = os.path.join(self.video_addon.path, self.video_addon.script)
        execfile(script_path, {'session':session,
                               'params':params,
                               '__file__':script_path,
                               'sys':self.__addon_sys, 'os':os})

    def _get_content_cb(self, success, result):
        log.debug('get_content_cb - success:%s result: %s' % (success, result))

        # resetting timeout for resolving content
        socket.setdefaulttimeout(socket.getdefaulttimeout())

        try:
            ssl._create_default_https_context = CREATE_DEFAULT_HTTPS_CONTEXT
        except:
            pass
        if success:
            log.debug("successfully loaded %d items" % len(self.__gui_item_list[0]))
            lst_itemscp = [[], None, {}]
            lst_itemscp[0] = self.__gui_item_list[0][:]
            lst_itemscp[1] = self.__gui_item_list[1]
            lst_itemscp[2] = self.__gui_item_list[2].copy()
            self.content_deferred.callback(lst_itemscp)
        else:
            self.content_deferred.errback(result)

    def is_seekable(self):
        return self.video_addon.get_setting('seekable')

    def is_pausable(self):
        return self.video_addon.get_setting('pausable')

    def close(self):
        self.video_addon = None
