import difflib
import os
import random
import re
import traceback
from functools import lru_cache

import zhconv
from lxml import etree

import log
from app.helper import MetaHelper
from app.media.meta.metainfo import MetaInfo
from app.media.tmdbv3api import TMDb, Search, Movie, TV, Person, Find, TMDbException
from app.utils import PathUtils, EpisodeFormat, RequestUtils, NumberUtils, StringUtils, cacheman
from app.utils.types import MediaType, MatchMode
from config import Config, KEYWORD_BLACKLIST, KEYWORD_SEARCH_WEIGHT_3, KEYWORD_SEARCH_WEIGHT_2, KEYWORD_SEARCH_WEIGHT_1, \
    KEYWORD_STR_SIMILARITY_THRESHOLD, KEYWORD_DIFF_SCORE_THRESHOLD, TMDB_IMAGE_ORIGINAL_URL, DEFAULT_TMDB_PROXY, \
    TMDB_IMAGE_FACE_URL, TMDB_PEOPLE_PROFILE_URL, TMDB_IMAGE_W500_URL


class Media:
    # TheMovieDB
    tmdb = None
    search = None
    movie = None
    tv = None
    person = None
    find = None
    meta = None
    _rmt_match_mode = None
    _search_keyword = None
    _search_tmdbweb = None

    def __init__(self):
        self.init_config()

    def init_config(self):
        app = Config().get_config('app')
        laboratory = Config().get_config('laboratory')
        if app:
            if app.get('rmt_tmdbkey'):
                self.tmdb = TMDb()
                if laboratory.get('tmdb_proxy'):
                    self.tmdb.domain = DEFAULT_TMDB_PROXY
                else:
                    self.tmdb.domain = app.get("tmdb_domain")
                self.tmdb.cache = True
                self.tmdb.api_key = app.get('rmt_tmdbkey')
                self.tmdb.language = 'zh'
                self.tmdb.proxies = Config().get_proxies()
                self.tmdb.debug = True
                self.search = Search()
                self.movie = Movie()
                self.tv = TV()
                self.find = Find()
                self.person = Person()
                self.meta = MetaHelper()
            rmt_match_mode = app.get('rmt_match_mode', 'normal')
            if rmt_match_mode:
                rmt_match_mode = rmt_match_mode.upper()
            else:
                rmt_match_mode = "NORMAL"
            if rmt_match_mode == "STRICT":
                self._rmt_match_mode = MatchMode.STRICT
            else:
                self._rmt_match_mode = MatchMode.NORMAL
        laboratory = Config().get_config('laboratory')
        if laboratory:
            self._search_keyword = laboratory.get("search_keyword")
            self._search_tmdbweb = laboratory.get("search_tmdbweb")

    @staticmethod
    def __compare_tmdb_names(file_name, tmdb_names):
        """
        ????????????????????????????????????????????????????????????
        :param file_name: ?????????????????????????????????
        :param tmdb_names: TMDB???????????????
        :return: True or False
        """
        if not file_name or not tmdb_names:
            return False
        if not isinstance(tmdb_names, list):
            tmdb_names = [tmdb_names]
        file_name = StringUtils.handler_special_chars(file_name).upper()
        for tmdb_name in tmdb_names:
            tmdb_name = StringUtils.handler_special_chars(tmdb_name).strip().upper()
            if file_name == tmdb_name:
                return True
        return False

    def __search_tmdb_allnames(self, mtype: MediaType, tmdb_id):
        """
        ??????tmdb????????????????????????????????????????????????
        :param mtype: ????????????????????????????????????
        :param tmdb_id: TMDB???ID
        :return: ?????????????????????
        """
        if not mtype or not tmdb_id:
            return {}, []
        ret_names = []
        tmdb_info = self.get_tmdb_info(mtype=mtype, tmdbid=tmdb_id)
        if not tmdb_info:
            return tmdb_info, []
        if mtype == MediaType.MOVIE:
            alternative_titles = tmdb_info.get("alternative_titles", {}).get("titles", [])
            for alternative_title in alternative_titles:
                title = alternative_title.get("title")
                if title and title not in ret_names:
                    ret_names.append(title)
            translations = tmdb_info.get("translations", {}).get("translations", [])
            for translation in translations:
                title = translation.get("data", {}).get("title")
                if title and title not in ret_names:
                    ret_names.append(title)
        else:
            alternative_titles = tmdb_info.get("alternative_titles", {}).get("results", [])
            for alternative_title in alternative_titles:
                name = alternative_title.get("title")
                if name and name not in ret_names:
                    ret_names.append(name)
            translations = tmdb_info.get("translations", {}).get("translations", [])
            for translation in translations:
                name = translation.get("data", {}).get("name")
                if name and name not in ret_names:
                    ret_names.append(name)
        return tmdb_info, ret_names

    def __search_tmdb(self, file_media_name,
                      search_type,
                      first_media_year=None,
                      media_year=None,
                      season_number=None,
                      language=None):
        """
        ??????tmdb???????????????????????????????????????????????????????????????
        :param file_media_name: ???????????????
        :param search_type: ????????????????????????????????????
        :param first_media_year: ?????????????????????????????????????????????(first_air_date)
        :param media_year: ??????????????????
        :param season_number: ???????????????
        :param language: ??????????????????zh-CN
        :return: TMDB???INFO???????????????search_type?????????media_type???
        """
        if not self.search:
            return None
        if not file_media_name:
            return None
        if language:
            self.tmdb.language = language
        else:
            self.tmdb.language = 'zh-CN'
        # TMDB??????
        info = {}
        if search_type == MediaType.MOVIE:
            year_range = [first_media_year]
            if first_media_year:
                year_range.append(str(int(first_media_year) + 1))
                year_range.append(str(int(first_media_year) - 1))
            for year in year_range:
                log.debug(
                    f"???Meta???????????????{search_type.value}???{file_media_name}, ??????={year} ...")
                info = self.__search_movie_by_name(file_media_name, year)
                if info:
                    info['media_type'] = MediaType.MOVIE
                    log.info("???Meta???%s ????????? ?????????TMDBID=%s, ??????=%s, ????????????=%s" % (
                        file_media_name,
                        info.get('id'),
                        info.get('title'),
                        info.get('release_date')))
                    break
        else:
            # ??????????????????????????????????????????????????????
            if media_year and season_number:
                log.debug(
                    f"???Meta???????????????{search_type.value}???{file_media_name}, ??????={season_number}, ????????????={media_year} ...")
                info = self.__search_tv_by_season(file_media_name,
                                                  media_year,
                                                  season_number)
            if not info:
                log.debug(
                    f"???Meta???????????????{search_type.value}???{file_media_name}, ??????={StringUtils.xstr(first_media_year)} ...")
                info = self.__search_tv_by_name(file_media_name,
                                                first_media_year)
            if info:
                info['media_type'] = MediaType.TV
                log.info("???Meta???%s ????????? ????????????TMDBID=%s, ??????=%s, ????????????=%s" % (
                    file_media_name,
                    info.get('id'),
                    info.get('name'),
                    info.get('first_air_date')))
        # ??????
        if info:
            return info
        else:
            log.info("???Meta???%s ????????? %s ???TMDB????????????%s??????!" % (
                file_media_name, StringUtils.xstr(first_media_year), search_type.value if search_type else ""))
            return info

    def __search_movie_by_name(self, file_media_name, first_media_year):
        """
        ????????????????????????TMDB??????
        :param file_media_name: ??????????????????????????????
        :param first_media_year: ??????????????????
        :return: ?????????????????????
        """
        try:
            if first_media_year:
                movies = self.search.movies({"query": file_media_name, "year": first_media_year})
            else:
                movies = self.search.movies({"query": file_media_name})
        except TMDbException as err:
            log.error(f"???Meta?????????TMDB?????????{str(err)}")
            return None
        except Exception as e:
            log.error(f"???Meta?????????TMDB?????????{str(e)}")
            return None
        log.debug(f"???Meta???API?????????{str(self.search.total_results)}")
        if len(movies) == 0:
            log.debug(f"???Meta???{file_media_name} ???????????????????????????!")
            return {}
        else:
            info = {}
            if first_media_year:
                for movie in movies:
                    if movie.get('release_date'):
                        if self.__compare_tmdb_names(file_media_name, movie.get('title')) \
                                and movie.get('release_date')[0:4] == str(first_media_year):
                            return movie
                        if self.__compare_tmdb_names(file_media_name, movie.get('original_title')) \
                                and movie.get('release_date')[0:4] == str(first_media_year):
                            return movie
            else:
                for movie in movies:
                    if self.__compare_tmdb_names(file_media_name, movie.get('title')) \
                            or self.__compare_tmdb_names(file_media_name, movie.get('original_title')):
                        return movie
            if not info:
                index = 0
                for movie in movies:
                    if first_media_year:
                        if not movie.get('release_date'):
                            continue
                        if movie.get('release_date')[0:4] != str(first_media_year):
                            continue
                        index += 1
                        info, names = self.__search_tmdb_allnames(MediaType.MOVIE, movie.get("id"))
                        if self.__compare_tmdb_names(file_media_name, names):
                            return info
                    else:
                        index += 1
                        info, names = self.__search_tmdb_allnames(MediaType.MOVIE, movie.get("id"))
                        if self.__compare_tmdb_names(file_media_name, names):
                            return info
                    if index > 5:
                        break
        return {}

    def __search_tv_by_name(self, file_media_name, first_media_year):
        """
        ???????????????????????????TMDB??????
        :param file_media_name: ?????????????????????????????????
        :param first_media_year: ????????????????????????
        :return: ?????????????????????
        """
        try:
            if first_media_year:
                tvs = self.search.tv_shows({"query": file_media_name, "first_air_date_year": first_media_year})
            else:
                tvs = self.search.tv_shows({"query": file_media_name})
        except TMDbException as err:
            log.error(f"???Meta?????????TMDB?????????{str(err)}")
            return None
        except Exception as e:
            log.error(f"???Meta?????????TMDB?????????{str(e)}")
            return None
        log.debug(f"???Meta???API?????????{str(self.search.total_results)}")
        if len(tvs) == 0:
            log.debug(f"???Meta???{file_media_name} ???????????????????????????!")
            return {}
        else:
            info = {}
            if first_media_year:
                for tv in tvs:
                    if tv.get('first_air_date'):
                        if self.__compare_tmdb_names(file_media_name, tv.get('name')) \
                                and tv.get('first_air_date')[0:4] == str(first_media_year):
                            return tv
                        if self.__compare_tmdb_names(file_media_name, tv.get('original_name')) \
                                and tv.get('first_air_date')[0:4] == str(first_media_year):
                            return tv
            else:
                for tv in tvs:
                    if self.__compare_tmdb_names(file_media_name, tv.get('name')) \
                            or self.__compare_tmdb_names(file_media_name, tv.get('original_name')):
                        return tv
            if not info:
                index = 0
                for tv in tvs:
                    if first_media_year:
                        if not tv.get('first_air_date'):
                            continue
                        if tv.get('first_air_date')[0:4] != str(first_media_year):
                            continue
                        index += 1
                        info, names = self.__search_tmdb_allnames(MediaType.TV, tv.get("id"))
                        if self.__compare_tmdb_names(file_media_name, names):
                            return info
                    else:
                        index += 1
                        info, names = self.__search_tmdb_allnames(MediaType.TV, tv.get("id"))
                        if self.__compare_tmdb_names(file_media_name, names):
                            return info
                    if index > 5:
                        break
        return {}

    def __search_tv_by_season(self, file_media_name, media_year, season_number):
        """
        ??????????????????????????????????????????????????????TMDB
        :param file_media_name: ?????????????????????????????????
        :param media_year: ????????????
        :param season_number: ?????????
        :return: ?????????????????????
        """

        def __season_match(tv_info, season_year):
            if not tv_info:
                return False
            try:
                seasons = self.get_tmdb_tv_seasons(tv_info=tv_info)
                for season in seasons:
                    if season.get("air_date") and season.get("season_number"):
                        if season.get("air_date")[0:4] == str(season_year) \
                                and season.get("season_number") == int(season_number):
                            return True
            except Exception as e1:
                log.error(f"???Meta?????????TMDB?????????{e1}")
                return False
            return False

        try:
            tvs = self.search.tv_shows({"query": file_media_name})
        except TMDbException as err:
            log.error(f"???Meta?????????TMDB?????????{str(err)}")
            return None
        except Exception as e:
            log.error(f"???Meta?????????TMDB?????????{e}")
            return None

        if len(tvs) == 0:
            log.debug("???Meta???%s ????????????%s????????????!" % (file_media_name, season_number))
            return {}
        else:
            for tv in tvs:
                if (self.__compare_tmdb_names(file_media_name, tv.get('name'))
                    or self.__compare_tmdb_names(file_media_name, tv.get('original_name'))) \
                        and (tv.get('first_air_date') and tv.get('first_air_date')[0:4] == str(media_year)):
                    return tv

            for tv in tvs[:5]:
                info, names = self.__search_tmdb_allnames(MediaType.TV, tv.get("id"))
                if not self.__compare_tmdb_names(file_media_name, names):
                    continue
                if __season_match(tv_info=info, season_year=media_year):
                    return info
        return {}

    def __search_multi_tmdb(self, file_media_name):
        """
        ?????????????????????????????????????????????????????????
        :param file_media_name: ??????????????????????????????
        :return: ?????????????????????
        """
        try:
            multis = self.search.multi({"query": file_media_name}) or []
        except TMDbException as err:
            log.error(f"???Meta?????????TMDB?????????{str(err)}")
            return None
        except Exception as e:
            log.error(f"???Meta?????????TMDB?????????{str(e)}")
            return None
        log.debug(f"???Meta???API?????????{str(self.search.total_results)}")
        if len(multis) == 0:
            log.debug(f"???Meta???{file_media_name} ????????????????????????!")
            return {}
        else:
            info = {}
            for multi in multis:
                if multi.get("media_type") == "movie":
                    if self.__compare_tmdb_names(file_media_name, multi.get('title')) \
                            or self.__compare_tmdb_names(file_media_name, multi.get('original_title')):
                        info = multi
                elif multi.get("media_type") == "tv":
                    if self.__compare_tmdb_names(file_media_name, multi.get('name')) \
                            or self.__compare_tmdb_names(file_media_name, multi.get('original_name')):
                        info = multi
            if not info:
                for multi in multis[:5]:
                    if multi.get("media_type") == "movie":
                        movie_info, names = self.__search_tmdb_allnames(MediaType.MOVIE, multi.get("id"))
                        if self.__compare_tmdb_names(file_media_name, names):
                            info = movie_info
                    elif multi.get("media_type") == "tv":
                        tv_info, names = self.__search_tmdb_allnames(MediaType.TV, multi.get("id"))
                        if self.__compare_tmdb_names(file_media_name, names):
                            info = tv_info
        # ??????
        if info:
            info['media_type'] = MediaType.MOVIE if info.get('media_type') == 'movie' else MediaType.TV
            return info
        else:
            log.info("???Meta???%s ???TMDB????????????????????????!" % file_media_name)
            return info

    @lru_cache(maxsize=128)
    def __search_tmdb_web(self, file_media_name, mtype: MediaType):
        """
        ??????TMDB????????????????????????????????????????????????????????????
        :param file_media_name: ??????
        """
        if not file_media_name:
            return None
        if StringUtils.is_chinese(file_media_name):
            return {}
        log.info("???Meta????????????TheDbMovie???????????????%s ..." % file_media_name)
        tmdb_url = "https://www.themoviedb.org/search?query=%s" % file_media_name
        res = RequestUtils(timeout=5).get_res(url=tmdb_url)
        if res and res.status_code == 200:
            html_text = res.text
            if not html_text:
                return None
            try:
                tmdb_links = []
                html = etree.HTML(html_text)
                links = html.xpath("//a[@data-id]/@href")
                for link in links:
                    if not link or (not link.startswith("/tv") and not link.startswith("/movie")):
                        continue
                    if link not in tmdb_links:
                        tmdb_links.append(link)
                if len(tmdb_links) == 1:
                    tmdbinfo = self.get_tmdb_info(
                        mtype=MediaType.TV if tmdb_links[0].startswith("/tv") else MediaType.MOVIE,
                        tmdbid=tmdb_links[0].split("/")[-1])
                    if tmdbinfo:
                        if mtype == MediaType.TV and tmdbinfo.get('media_type') != MediaType.TV:
                            return {}
                        if tmdbinfo.get('media_type') == MediaType.MOVIE:
                            log.info("???Meta???%s ???WEB????????? ?????????TMDBID=%s, ??????=%s, ????????????=%s" % (
                                file_media_name,
                                tmdbinfo.get('id'),
                                tmdbinfo.get('title'),
                                tmdbinfo.get('release_date')))
                        else:
                            log.info("???Meta???%s ???WEB????????? ????????????TMDBID=%s, ??????=%s, ????????????=%s" % (
                                file_media_name,
                                tmdbinfo.get('id'),
                                tmdbinfo.get('name'),
                                tmdbinfo.get('first_air_date')))
                    return tmdbinfo
                elif len(tmdb_links) > 1:
                    log.info("???Meta???%s TMDB???????????????????????????%s" % (file_media_name, len(tmdb_links)))
                else:
                    log.info("???Meta???%s TMDB?????????????????????????????????" % file_media_name)
            except Exception as err:
                print(str(err))
                return None
        return None

    def get_tmdb_info(self, mtype: MediaType,
                      tmdbid,
                      language=None,
                      append_to_response=None,
                      chinese=True):
        """
        ??????TMDB??????????????????????????????
        :param mtype: ?????????????????????????????????????????????????????????????????????????????????
        :param tmdbid: TMDB???ID??????tmdbid???????????????tmdbid??????????????????????????????
        :param language: ??????
        :param append_to_response: ????????????
        :param chinese: ????????????????????????
        """
        if not self.tmdb:
            log.error("???Meta???TMDB API Key ????????????")
            return None
        if language:
            self.tmdb.language = language
        else:
            self.tmdb.language = 'zh-CN'
        if mtype == MediaType.MOVIE:
            tmdb_info = self.__get_tmdb_movie_detail(tmdbid, append_to_response)
            if tmdb_info:
                tmdb_info['media_type'] = MediaType.MOVIE
        else:
            tmdb_info = self.__get_tmdb_tv_detail(tmdbid, append_to_response)
            if tmdb_info:
                tmdb_info['media_type'] = MediaType.TV
        if tmdb_info:
            # ??????genreid
            tmdb_info['genre_ids'] = self.__get_genre_ids_from_detail(tmdb_info.get('genres'))
            # ??????????????????
            if chinese:
                tmdb_info = self.__update_tmdbinfo_cn_title(tmdb_info)

        return tmdb_info

    def __update_tmdbinfo_cn_title(self, tmdb_info):
        """
        ??????TMDB????????????????????????
        """
        # ???????????????
        org_title = tmdb_info.get("title") if tmdb_info.get("media_type") == MediaType.MOVIE else tmdb_info.get(
            "name")
        if not StringUtils.is_chinese(org_title) and self.tmdb.language == 'zh-CN':
            cn_title = self.__get_tmdb_chinese_title(tmdbinfo=tmdb_info)
            if cn_title and cn_title != org_title:
                if tmdb_info.get("media_type") == MediaType.MOVIE:
                    tmdb_info['title'] = cn_title
                else:
                    tmdb_info['name'] = cn_title
        return tmdb_info

    def get_tmdb_infos(self, title, year=None, mtype: MediaType = None, page=1):
        """
        ???????????????????????????????????????TMDB???????????????
        """
        if not self.tmdb:
            log.error("???Meta???TMDB API Key ????????????")
            return []
        if not title:
            return []
        if not mtype and not year:
            results = self.__search_multi_tmdbinfos(title)
        else:
            if not mtype:
                results = list(
                    set(self.__search_movie_tmdbinfos(title, year)).union(set(self.__search_tv_tmdbinfos(title, year))))
                # ?????????????????????????????????
                results = sorted(results,
                                 key=lambda x: x.get("release_date") or x.get("first_air_date") or "0000-00-00",
                                 reverse=True)
            elif mtype == MediaType.MOVIE:
                results = self.__search_movie_tmdbinfos(title, year)
            else:
                results = self.__search_tv_tmdbinfos(title, year)
        return results[(page - 1) * 20:page * 20]

    def __search_multi_tmdbinfos(self, title):
        """
        ?????????????????????????????????????????????TMDB??????
        """
        if not title:
            return []
        ret_infos = []
        multis = self.search.multi({"query": title}) or []
        for multi in multis:
            if multi.get("media_type") in ["movie", "tv"]:
                multi['media_type'] = MediaType.MOVIE if multi.get("media_type") == "movie" else MediaType.TV
                ret_infos.append(multi)
        return ret_infos

    def __search_movie_tmdbinfos(self, title, year):
        """
        ?????????????????????????????????TMDB??????
        """
        if not title:
            return []
        ret_infos = []
        if year:
            movies = self.search.movies({"query": title, "year": year}) or []
        else:
            movies = self.search.movies({"query": title}) or []
        for movie in movies:
            if title in movie.get("title"):
                movie['media_type'] = MediaType.MOVIE
                ret_infos.append(movie)
        return ret_infos

    def __search_tv_tmdbinfos(self, title, year):
        """
        ????????????????????????????????????TMDB??????
        """
        if not title:
            return []
        ret_infos = []
        if year:
            tvs = self.search.tv_shows({"query": title, "first_air_date_year": year}) or []
        else:
            tvs = self.search.tv_shows({"query": title}) or []
        for tv in tvs:
            if title in tv.get("name"):
                tv['media_type'] = MediaType.TV
                ret_infos.append(tv)
        return ret_infos

    @staticmethod
    def __make_cache_key(meta_info):
        """
        ???????????????key
        """
        if not meta_info:
            return None
        return f"[{meta_info.type.value}]{meta_info.get_name()}-{meta_info.year}-{meta_info.begin_season}"

    def get_cache_info(self, meta_info):
        """
        ???????????????????????????????????????
        """
        if not meta_info:
            return {}
        return self.meta.get_meta_data_by_key(self.__make_cache_key(meta_info))

    def get_media_info(self, title,
                       subtitle=None,
                       mtype=None,
                       strict=None,
                       cache=True,
                       chinese=True,
                       append_to_response=None):
        """
        ????????????????????????????????????????????????????????????TMDB?????????????????????????????????
        :param title: ????????????
        :param subtitle: ???????????????
        :param mtype: ????????????????????????????????????
        :param strict: ????????????????????????true???????????????????????????????????????
        :param cache: ???????????????????????????TRUE
        :param chinese: ?????????????????????????????????????????????????????????
        :param append_to_response: ?????????????????????
        :return: ??????TMDB?????????MetaInfo??????
        """
        if not self.tmdb:
            log.error("???Meta???TMDB API Key ????????????")
            return None
        if not title:
            return None
        # ??????
        meta_info = MetaInfo(title, subtitle=subtitle)
        if not meta_info.get_name() or not meta_info.type:
            log.warn("???Rmt???%s ???????????????????????????" % meta_info.org_string)
            return None
        if mtype:
            meta_info.type = mtype
        media_key = self.__make_cache_key(meta_info)
        if not cache or not self.meta.get_meta_data_by_key(media_key):
            # ???????????????????????????????????????
            if meta_info.type != MediaType.TV and not meta_info.year:
                file_media_info = self.__search_multi_tmdb(file_media_name=meta_info.get_name())
            else:
                if meta_info.type == MediaType.TV:
                    # ???????????????
                    file_media_info = self.__search_tmdb(file_media_name=meta_info.get_name(),
                                                         first_media_year=meta_info.year,
                                                         search_type=meta_info.type,
                                                         media_year=meta_info.year,
                                                         season_number=meta_info.begin_season
                                                         )
                    if not file_media_info and meta_info.year and self._rmt_match_mode == MatchMode.NORMAL and not strict:
                        # ??????????????????????????????????????????
                        file_media_info = self.__search_tmdb(file_media_name=meta_info.get_name(),
                                                             search_type=meta_info.type
                                                             )
                else:
                    # ????????????????????????
                    file_media_info = self.__search_tmdb(file_media_name=meta_info.get_name(),
                                                         first_media_year=meta_info.year,
                                                         search_type=MediaType.MOVIE
                                                         )
                    # ????????????????????????
                    if not file_media_info:
                        file_media_info = self.__search_tmdb(file_media_name=meta_info.get_name(),
                                                             first_media_year=meta_info.year,
                                                             search_type=MediaType.TV
                                                             )
                    if not file_media_info and self._rmt_match_mode == MatchMode.NORMAL and not strict:
                        # ???????????????????????????????????????????????????
                        file_media_info = self.__search_multi_tmdb(file_media_name=meta_info.get_name())
            if not file_media_info and self._search_tmdbweb:
                file_media_info = self.__search_tmdb_web(file_media_name=meta_info.get_name(),
                                                         mtype=meta_info.type)
            if not file_media_info and self._search_keyword:
                cache_name = cacheman["tmdb_supply"].get(meta_info.get_name())
                is_movie = False
                if not cache_name:
                    cache_name, is_movie = self.__search_engine(meta_info.get_name())
                    cacheman["tmdb_supply"].set(meta_info.get_name(), cache_name)
                if cache_name:
                    log.info("???Meta????????????????????????%s ..." % cache_name)
                    if is_movie:
                        file_media_info = self.__search_tmdb(file_media_name=cache_name, search_type=MediaType.MOVIE)
                    else:
                        file_media_info = self.__search_multi_tmdb(file_media_name=cache_name)
            # ??????????????????
            if file_media_info and not file_media_info.get("genres"):
                file_media_info = self.get_tmdb_info(mtype=file_media_info.get("media_type"),
                                                     tmdbid=file_media_info.get("id"),
                                                     chinese=chinese,
                                                     append_to_response=append_to_response)
            # ???????????????
            if file_media_info is not None:
                self.__insert_media_cache(media_key=media_key,
                                          file_media_info=file_media_info)
        else:
            # ??????????????????
            cache_info = self.meta.get_meta_data_by_key(media_key)
            if cache_info.get("id"):
                file_media_info = self.get_tmdb_info(mtype=cache_info.get("type"),
                                                     tmdbid=cache_info.get("id"),
                                                     chinese=chinese,
                                                     append_to_response=append_to_response)
            else:
                file_media_info = None
        # ??????TMDB???????????????
        meta_info.set_tmdb_info(file_media_info)
        return meta_info

    def __insert_media_cache(self, media_key, file_media_info):
        """
        ???TMDB??????????????????
        """
        if file_media_info:
            # ????????????
            cache_title = file_media_info.get(
                "title") if file_media_info.get(
                "media_type") == MediaType.MOVIE else file_media_info.get("name")
            # ????????????
            cache_year = file_media_info.get('release_date') if file_media_info.get(
                "media_type") == MediaType.MOVIE else file_media_info.get('first_air_date')
            if cache_year:
                cache_year = cache_year[:4]
            self.meta.update_meta_data({
                media_key: {
                    "id": file_media_info.get("id"),
                    "type": file_media_info.get("media_type"),
                    "year": cache_year,
                    "title": cache_title,
                    "poster_path": file_media_info.get("poster_path"),
                    "backdrop_path": file_media_info.get("backdrop_path")
                }
            })
        else:
            self.meta.update_meta_data({media_key: {'id': 0}})

    def get_media_info_on_files(self,
                                file_list,
                                tmdb_info=None,
                                media_type=None,
                                season=None,
                                episode_format: EpisodeFormat = None,
                                chinese=True):
        """
        ???????????????????????????TMDB????????????????????????????????????
        :param file_list: ?????????????????????????????????????????????????????????????????????????????????
        :param tmdb_info: ????????????TMDB???????????????TMDB?????????????????????????????????????????????TMDB????????????????????????????????????
        :param media_type: ????????????????????????????????????????????????????????????????????????????????????????????????????????????TMDB???????????????
        :param season: ??????????????????????????????????????????????????????????????????????????????
        :param episode_format: EpisodeFormat
        :param chinese: ?????????????????????????????????????????????????????????
        :return: ??????TMDB??????????????????????????????MetaInfo????????????
        """
        # ??????????????????????????????????????????
        if not self.tmdb:
            log.error("???Meta???TMDB API Key ????????????")
            return {}
        return_media_infos = {}
        # ??????list?????????list
        if not isinstance(file_list, list):
            file_list = [file_list]
        # ????????????????????????????????????????????????????????????????????????????????????????????????
        for file_path in file_list:
            try:
                if not os.path.exists(file_path):
                    log.warn("???Meta???%s ?????????" % file_path)
                    continue
                # ??????????????????
                # ?????????????????????
                file_name = os.path.basename(file_path)
                parent_name = os.path.basename(os.path.dirname(file_path))
                parent_parent_name = os.path.basename(PathUtils.get_parent_paths(file_path, 2))
                # ??????????????????????????????????????????
                if not os.path.isdir(file_path) \
                        and PathUtils.get_bluray_dir(file_path):
                    log.info("???Meta???%s ???????????????????????????" % file_path)
                    continue
                # ????????????TMDB??????
                if not tmdb_info:
                    # ????????????
                    meta_info = MetaInfo(title=file_name)
                    # ????????????????????????????????????
                    if not meta_info.get_name() or not meta_info.year:
                        parent_info = MetaInfo(parent_name)
                        if not parent_info.get_name() or not parent_info.year:
                            parent_parent_info = MetaInfo(parent_parent_name)
                            parent_info.type = parent_parent_info.type if parent_parent_info.type and parent_info.type != MediaType.TV else parent_info.type
                            parent_info.cn_name = parent_parent_info.cn_name if parent_parent_info.cn_name else parent_info.cn_name
                            parent_info.en_name = parent_parent_info.en_name if parent_parent_info.en_name else parent_info.en_name
                            parent_info.year = parent_parent_info.year if parent_parent_info.year else parent_info.year
                            parent_info.begin_season = NumberUtils.max_ele(parent_info.begin_season,
                                                                           parent_parent_info.begin_season)
                        if not meta_info.get_name():
                            meta_info.cn_name = parent_info.cn_name
                            meta_info.en_name = parent_info.en_name
                        if not meta_info.year:
                            meta_info.year = parent_info.year
                        if parent_info.type and parent_info.type == MediaType.TV \
                                and meta_info.type != MediaType.TV:
                            meta_info.type = parent_info.type
                        if meta_info.type == MediaType.TV:
                            meta_info.begin_season = NumberUtils.max_ele(parent_info.begin_season,
                                                                         meta_info.begin_season)
                    if not meta_info.get_name() or not meta_info.type:
                        log.warn("???Rmt???%s ???????????????????????????" % meta_info.org_string)
                        continue
                    # ???????????????TMDB
                    media_key = self.__make_cache_key(meta_info)
                    if not self.meta.get_meta_data_by_key(media_key):
                        # ??????????????????
                        file_media_info = self.__search_tmdb(file_media_name=meta_info.get_name(),
                                                             first_media_year=meta_info.year,
                                                             search_type=meta_info.type,
                                                             media_year=meta_info.year,
                                                             season_number=meta_info.begin_season)
                        if not file_media_info:
                            if self._rmt_match_mode == MatchMode.NORMAL:
                                # ???????????????????????????????????????????????????
                                file_media_info = self.__search_tmdb(file_media_name=meta_info.get_name(),
                                                                     search_type=meta_info.type)
                        if not file_media_info and self._search_tmdbweb:
                            # ???????????????
                            file_media_info = self.__search_tmdb_web(file_media_name=meta_info.get_name(),
                                                                     mtype=meta_info.type)
                        if not file_media_info and self._search_keyword:
                            cache_name = cacheman["tmdb_supply"].get(meta_info.get_name())
                            is_movie = False
                            if not cache_name:
                                cache_name, is_movie = self.__search_engine(meta_info.get_name())
                                cacheman["tmdb_supply"].set(meta_info.get_name(), cache_name)
                            if cache_name:
                                log.info("???Meta????????????????????????%s ..." % cache_name)
                                if is_movie:
                                    file_media_info = self.__search_tmdb(file_media_name=cache_name,
                                                                         search_type=MediaType.MOVIE)
                                else:
                                    file_media_info = self.__search_multi_tmdb(file_media_name=cache_name)
                        # ??????TMDB??????
                        if file_media_info and not file_media_info.get("genres"):
                            file_media_info = self.get_tmdb_info(mtype=file_media_info.get("media_type"),
                                                                 tmdbid=file_media_info.get("id"),
                                                                 chinese=chinese)
                        # ???????????????
                        if file_media_info is not None:
                            self.__insert_media_cache(media_key=media_key,
                                                      file_media_info=file_media_info)
                    else:
                        # ??????????????????
                        cache_info = self.meta.get_meta_data_by_key(media_key)
                        if cache_info.get("id"):
                            file_media_info = self.get_tmdb_info(mtype=cache_info.get("type"),
                                                                 tmdbid=cache_info.get("id"),
                                                                 chinese=chinese)
                        else:
                            # ??????????????????
                            file_media_info = None
                    # ??????TMDB??????
                    meta_info.set_tmdb_info(file_media_info)
                # ??????TMDB??????
                else:
                    meta_info = MetaInfo(title=file_name, mtype=media_type)
                    meta_info.set_tmdb_info(tmdb_info)
                    if season and meta_info.type != MediaType.MOVIE:
                        meta_info.begin_season = int(season)
                    if episode_format:
                        begin_ep, end_ep = episode_format.split_episode(file_name)
                        if begin_ep is not None:
                            meta_info.begin_episode = begin_ep
                        if end_ep is not None:
                            meta_info.end_episode = end_ep
                    # ????????????
                    self.save_rename_cache(file_name, tmdb_info)
                # ?????????????????????
                return_media_infos[file_path] = meta_info
            except Exception as err:
                print(str(err))
                log.error("???Rmt??????????????????%s - %s" % (str(err), traceback.format_exc()))
        # ????????????
        return return_media_infos

    @staticmethod
    def __dict_tvinfos(tvs):
        """
        TMDB???????????????????????????
        """
        return [{
            'id': tv.get("id"),
            'orgid': tv.get("id"),
            'tmdbid': tv.get("id"),
            'title': tv.get("name"),
            'type': 'TV',
            'media_type': MediaType.TV.value,
            'year': tv.get("first_air_date")[0:4] if tv.get("first_air_date") else "",
            'vote': round(float(tv.get("vote_average")), 1) if tv.get("vote_average") else 0,
            'image': TMDB_IMAGE_W500_URL % tv.get("poster_path"),
            'overview': tv.get("overview")
        } for tv in tvs or []]

    @staticmethod
    def __dict_movieinfos(movies):
        """
        TMDB????????????????????????
        """
        return [{
            'id': movie.get("id"),
            'orgid': movie.get("id"),
            'tmdbid': movie.get("id"),
            'title': movie.get("title"),
            'type': 'MOV',
            'media_type': MediaType.MOVIE.value,
            'year': movie.get("release_date")[0:4] if movie.get("release_date") else "",
            'vote': round(float(movie.get("vote_average")), 1) if movie.get("vote_average") else 0,
            'image': TMDB_IMAGE_W500_URL % movie.get("poster_path"),
            'overview': movie.get("overview")
        } for movie in movies or []]

    def get_tmdb_hot_movies(self, page):
        """
        ??????????????????
        :param page: ?????????
        :return: TMDB????????????
        """
        if not self.movie:
            return []
        return self.__dict_movieinfos(self.movie.popular(page))

    def get_tmdb_hot_tvs(self, page):
        """
        ?????????????????????
        :param page: ?????????
        :return: TMDB????????????
        """
        if not self.tv:
            return []
        return self.__dict_tvinfos(self.tv.popular(page))

    def get_tmdb_new_movies(self, page):
        """
        ??????????????????
        :param page: ?????????
        :return: TMDB????????????
        """
        if not self.movie:
            return []
        return self.__dict_movieinfos(self.movie.now_playing(page))

    def get_tmdb_new_tvs(self, page):
        """
        ?????????????????????
        :param page: ?????????
        :return: TMDB????????????
        """
        if not self.tv:
            return []
        return self.__dict_tvinfos(self.tv.on_the_air(page))

    def get_tmdb_upcoming_movies(self, page):
        """
        ????????????????????????
        :param page: ?????????
        :return: TMDB????????????
        """
        if not self.movie:
            return []
        return self.__dict_movieinfos(self.movie.upcoming(page))

    def __get_tmdb_movie_detail(self, tmdbid, append_to_response=None):
        """
        ?????????????????????
        :param tmdbid: TMDB ID
        :return: TMDB??????
        """
        """
        {
          "adult": false,
          "backdrop_path": "/r9PkFnRUIthgBp2JZZzD380MWZy.jpg",
          "belongs_to_collection": {
            "id": 94602,
            "name": "???????????????????????????",
            "poster_path": "/anHwj9IupRoRZZ98WTBvHpTiE6A.jpg",
            "backdrop_path": "/feU1DWV5zMWxXUHJyAIk3dHRQ9c.jpg"
          },
          "budget": 90000000,
          "genres": [
            {
              "id": 16,
              "name": "??????"
            },
            {
              "id": 28,
              "name": "??????"
            },
            {
              "id": 12,
              "name": "??????"
            },
            {
              "id": 35,
              "name": "??????"
            },
            {
              "id": 10751,
              "name": "??????"
            },
            {
              "id": 14,
              "name": "??????"
            }
          ],
          "homepage": "",
          "id": 315162,
          "imdb_id": "tt3915174",
          "original_language": "en",
          "original_title": "Puss in Boots: The Last Wish",
          "overview": "??????11????????????????????????????????????????????????????????????????????????????????????????????????????? ????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????? ?????????????????????????????????????????????????????????",
          "popularity": 8842.129,
          "poster_path": "/rnn30OlNPiC3IOoWHKoKARGsBRK.jpg",
          "production_companies": [
            {
              "id": 33,
              "logo_path": "/8lvHyhjr8oUKOOy2dKXoALWKdp0.png",
              "name": "Universal Pictures",
              "origin_country": "US"
            },
            {
              "id": 521,
              "logo_path": "/kP7t6RwGz2AvvTkvnI1uteEwHet.png",
              "name": "DreamWorks Animation",
              "origin_country": "US"
            }
          ],
          "production_countries": [
            {
              "iso_3166_1": "US",
              "name": "United States of America"
            }
          ],
          "release_date": "2022-12-07",
          "revenue": 260725470,
          "runtime": 102,
          "spoken_languages": [
            {
              "english_name": "English",
              "iso_639_1": "en",
              "name": "English"
            },
            {
              "english_name": "Spanish",
              "iso_639_1": "es",
              "name": "Espa??ol"
            }
          ],
          "status": "Released",
          "tagline": "",
          "title": "???????????????2",
          "video": false,
          "vote_average": 8.614,
          "vote_count": 2291
        }
        """
        if not self.movie:
            return {}
        try:
            log.info("???Meta???????????????TMDB?????????%s ..." % tmdbid)
            tmdbinfo = self.movie.details(tmdbid, append_to_response)
            return tmdbinfo or {}
        except Exception as e:
            print(str(e))
            return None

    def __get_tmdb_tv_detail(self, tmdbid, append_to_response=None):
        """
        ????????????????????????
        :param tmdbid: TMDB ID
        :return: TMDB??????
        """
        """
        {
          "adult": false,
          "backdrop_path": "/uDgy6hyPd82kOHh6I95FLtLnj6p.jpg",
          "created_by": [
            {
              "id": 35796,
              "credit_id": "5e84f06a3344c600153f6a57",
              "name": "Craig Mazin",
              "gender": 2,
              "profile_path": "/uEhna6qcMuyU5TP7irpTUZ2ZsZc.jpg"
            },
            {
              "id": 1295692,
              "credit_id": "5e84f03598f1f10016a985c0",
              "name": "Neil Druckmann",
              "gender": 2,
              "profile_path": "/bVUsM4aYiHbeSYE1xAw2H5Z1ANU.jpg"
            }
          ],
          "episode_run_time": [],
          "first_air_date": "2023-01-15",
          "genres": [
            {
              "id": 18,
              "name": "??????"
            },
            {
              "id": 10765,
              "name": "Sci-Fi & Fantasy"
            },
            {
              "id": 10759,
              "name": "????????????"
            }
          ],
          "homepage": "https://www.hbo.com/the-last-of-us",
          "id": 100088,
          "in_production": true,
          "languages": [
            "en"
          ],
          "last_air_date": "2023-01-15",
          "last_episode_to_air": {
            "air_date": "2023-01-15",
            "episode_number": 1,
            "id": 2181581,
            "name": "????????????????????????",
            "overview": "???????????????????????????????????????????????????????????????????????????????????????????????? 14 ??????????????????????????????????????????????????????",
            "production_code": "",
            "runtime": 81,
            "season_number": 1,
            "show_id": 100088,
            "still_path": "/aRquEWm8wWF1dfa9uZ1TXLvVrKD.jpg",
            "vote_average": 8,
            "vote_count": 33
          },
          "name": "???????????????",
          "next_episode_to_air": {
            "air_date": "2023-01-22",
            "episode_number": 2,
            "id": 4071039,
            "name": "???????????????",
            "overview": "",
            "production_code": "",
            "runtime": 55,
            "season_number": 1,
            "show_id": 100088,
            "still_path": "/jkUtYTmeap6EvkHI4n0j5IRFrIr.jpg",
            "vote_average": 10,
            "vote_count": 1
          },
          "networks": [
            {
              "id": 49,
              "name": "HBO",
              "logo_path": "/tuomPhY2UtuPTqqFnKMVHvSb724.png",
              "origin_country": "US"
            }
          ],
          "number_of_episodes": 9,
          "number_of_seasons": 1,
          "origin_country": [
            "US"
          ],
          "original_language": "en",
          "original_name": "The Last of Us",
          "overview": "??????????????????????????????????????????????????????????????????????????????????????????????????????Joel???????????????????????????????????????????????????Ellie???????????????????????????????????????????????????????????????????????????",
          "popularity": 5585.639,
          "poster_path": "/nOY3VBFO0VnlN9nlRombnMTztyh.jpg",
          "production_companies": [
            {
              "id": 3268,
              "logo_path": "/tuomPhY2UtuPTqqFnKMVHvSb724.png",
              "name": "HBO",
              "origin_country": "US"
            },
            {
              "id": 11073,
              "logo_path": "/aCbASRcI1MI7DXjPbSW9Fcv9uGR.png",
              "name": "Sony Pictures Television Studios",
              "origin_country": "US"
            },
            {
              "id": 23217,
              "logo_path": "/kXBZdQigEf6QiTLzo6TFLAa7jKD.png",
              "name": "Naughty Dog",
              "origin_country": "US"
            },
            {
              "id": 115241,
              "logo_path": null,
              "name": "The Mighty Mint",
              "origin_country": "US"
            },
            {
              "id": 119645,
              "logo_path": null,
              "name": "Word Games",
              "origin_country": "US"
            },
            {
              "id": 125281,
              "logo_path": "/3hV8pyxzAJgEjiSYVv1WZ0ZYayp.png",
              "name": "PlayStation Productions",
              "origin_country": "US"
            }
          ],
          "production_countries": [
            {
              "iso_3166_1": "US",
              "name": "United States of America"
            }
          ],
          "seasons": [
            {
              "air_date": "2023-01-15",
              "episode_count": 9,
              "id": 144593,
              "name": "??? 1 ???",
              "overview": "",
              "poster_path": "/aUQKIpZZ31KWbpdHMCmaV76u78T.jpg",
              "season_number": 1
            }
          ],
          "spoken_languages": [
            {
              "english_name": "English",
              "iso_639_1": "en",
              "name": "English"
            }
          ],
          "status": "Returning Series",
          "tagline": "",
          "type": "Scripted",
          "vote_average": 8.924,
          "vote_count": 601
        }
        """
        if not self.tv:
            return {}
        try:
            log.info("???Meta???????????????TMDB????????????%s ..." % tmdbid)
            tmdbinfo = self.tv.details(tmdbid, append_to_response)
            return tmdbinfo or {}
        except Exception as e:
            print(str(e))
            return None

    def get_tmdb_tv_season_detail(self, tmdbid, season: int):
        """
        ???????????????????????????
        :param tmdbid: TMDB ID
        :param season: ????????????
        :return: TMDB??????
        """
        """
        {
          "_id": "5e614cd3357c00001631a6ef",
          "air_date": "2023-01-15",
          "episodes": [
            {
              "air_date": "2023-01-15",
              "episode_number": 1,
              "id": 2181581,
              "name": "????????????????????????",
              "overview": "???????????????????????????????????????????????????????????????????????????????????????????????? 14 ??????????????????????????????????????????????????????",
              "production_code": "",
              "runtime": 81,
              "season_number": 1,
              "show_id": 100088,
              "still_path": "/aRquEWm8wWF1dfa9uZ1TXLvVrKD.jpg",
              "vote_average": 8,
              "vote_count": 33,
              "crew": [
                {
                  "job": "Writer",
                  "department": "Writing",
                  "credit_id": "619c370063536a00619a08ee",
                  "adult": false,
                  "gender": 2,
                  "id": 35796,
                  "known_for_department": "Writing",
                  "name": "Craig Mazin",
                  "original_name": "Craig Mazin",
                  "popularity": 15.211,
                  "profile_path": "/uEhna6qcMuyU5TP7irpTUZ2ZsZc.jpg"
                },
              ],
              "guest_stars": [
                {
                  "character": "Marlene",
                  "credit_id": "63c4ca5e5f2b8d00aed539fc",
                  "order": 500,
                  "adult": false,
                  "gender": 1,
                  "id": 1253388,
                  "known_for_department": "Acting",
                  "name": "Merle Dandridge",
                  "original_name": "Merle Dandridge",
                  "popularity": 21.679,
                  "profile_path": "/lKwHdTtDf6NGw5dUrSXxbfkZLEk.jpg"
                }
              ]
            },
          ],
          "name": "??? 1 ???",
          "overview": "",
          "id": 144593,
          "poster_path": "/aUQKIpZZ31KWbpdHMCmaV76u78T.jpg",
          "season_number": 1
        }
        """
        if not self.tv:
            return {}
        try:
            log.info("???Meta???????????????TMDB????????????%s?????????%s ..." % (tmdbid, season))
            tmdbinfo = self.tv.season_details(tmdbid, season)
            return tmdbinfo or {}
        except Exception as e:
            print(str(e))
            return {}

    def get_tmdb_tv_seasons_byid(self, tmdbid):
        """
        ??????TMDB??????TMDB?????????????????????
        """
        if not tmdbid:
            return []
        return self.get_tmdb_tv_seasons(
            tv_info=self.__get_tmdb_tv_detail(
                tmdbid=tmdbid
            )
        )

    @staticmethod
    def get_tmdb_tv_seasons(tv_info):
        """
        ??????TMDB?????????????????????
        :param tv_info: TMDB ????????????
        :return: ??????season_number???episode_count ?????????????????????????????????
        """
        """
        "seasons": [
            {
              "air_date": "2006-01-08",
              "episode_count": 11,
              "id": 3722,
              "name": "?????????",
              "overview": "",
              "poster_path": "/snQYndfsEr3Sto2jOmkmsQuUXAQ.jpg",
              "season_number": 0
            },
            {
              "air_date": "2005-03-27",
              "episode_count": 9,
              "id": 3718,
              "name": "??? 1 ???",
              "overview": "",
              "poster_path": "/foM4ImvUXPrD2NvtkHyixq5vhPx.jpg",
              "season_number": 1
            }
        ]
        """
        if not tv_info:
            return []
        return tv_info.get("seasons") or []

    def get_tmdb_season_episodes(self, tmdbid, season: int):
        """
        :param: tmdbid: TMDB ID
        :param: season: ??????
        """
        """
        ???TMDB??????????????????????????????????????????
        """
        """
        "episodes": [
            {
              "air_date": "2023-01-15",
              "episode_number": 1,
              "id": 2181581,
              "name": "????????????????????????",
              "overview": "???????????????????????????????????????????????????????????????????????????????????????????????? 14 ??????????????????????????????????????????????????????",
              "production_code": "",
              "runtime": 81,
              "season_number": 1,
              "show_id": 100088,
              "still_path": "/aRquEWm8wWF1dfa9uZ1TXLvVrKD.jpg",
              "vote_average": 8,
              "vote_count": 33
            },
          ]
        """
        if not tmdbid:
            return []
        season_info = self.get_tmdb_tv_season_detail(tmdbid=tmdbid, season=season)
        if not season_info:
            return []
        return season_info.get("episodes") or []

    @staticmethod
    def get_tmdb_backdrops(tmdbinfo):
        """
        ??????TMDB????????????
        """
        """
        {
          "backdrops": [
            {
              "aspect_ratio": 1.778,
              "height": 2160,
              "iso_639_1": "en",
              "file_path": "/qUroDlCDUMwRWbkyjZGB9THkMgZ.jpg",
              "vote_average": 5.312,
              "vote_count": 1,
              "width": 3840
            },
            {
              "aspect_ratio": 1.778,
              "height": 2160,
              "iso_639_1": "en",
              "file_path": "/iyxvxEQIfQjzJJTfszZxmH5UV35.jpg",
              "vote_average": 0,
              "vote_count": 0,
              "width": 3840
            },
            {
              "aspect_ratio": 1.778,
              "height": 720,
              "iso_639_1": "en",
              "file_path": "/8SRY6IcMKO1E5p83w7bjvcqklp9.jpg",
              "vote_average": 0,
              "vote_count": 0,
              "width": 1280
            },
            {
              "aspect_ratio": 1.778,
              "height": 1080,
              "iso_639_1": "en",
              "file_path": "/erkJ7OxJWFdLBOcn2MvIdhTLHTu.jpg",
              "vote_average": 0,
              "vote_count": 0,
              "width": 1920
            }
          ]
        }
        """
        if not tmdbinfo:
            return []
        backdrops = tmdbinfo.get("images", {}).get("backdrops") or []
        result = [TMDB_IMAGE_ORIGINAL_URL % backdrop.get("file_path") for backdrop in backdrops]
        result.append(TMDB_IMAGE_ORIGINAL_URL % tmdbinfo.get("backdrop_path"))
        return result

    @staticmethod
    def get_tmdb_season_episodes_num(tv_info, season: int):
        """
        ???TMDB??????????????????????????????????????????
        :param season: ???????????????
        :param tv_info: ????????????TMDB????????????
        :return: ??????????????????
        """
        if not tv_info:
            return 0
        seasons = tv_info.get("seasons")
        if not seasons:
            return 0
        for sea in seasons:
            if sea.get("season_number") == int(season):
                return int(sea.get("episode_count"))
        return 0

    @staticmethod
    def __dict_media_crews(crews):
        """
        ???????????????????????????
        """
        return [{
            "id": crew.get("id"),
            "gender": crew.get("gender"),
            "known_for_department": crew.get("known_for_department"),
            "name": crew.get("name"),
            "original_name": crew.get("original_name"),
            "popularity": crew.get("popularity"),
            "image": TMDB_IMAGE_FACE_URL % crew.get("profile_path"),
            "credit_id": crew.get("credit_id"),
            "department": crew.get("department"),
            "job": crew.get("job"),
            "profile": TMDB_PEOPLE_PROFILE_URL % crew.get('id')
        } for crew in crews or []]

    @staticmethod
    def __dict_media_casts(casts):
        """
        ???????????????????????????
        """
        return [{
            "id": cast.get("id"),
            "gender": cast.get("gender"),
            "known_for_department": cast.get("known_for_department"),
            "name": cast.get("name"),
            "original_name": cast.get("original_name"),
            "popularity": cast.get("popularity"),
            "image": TMDB_IMAGE_FACE_URL % cast.get("profile_path"),
            "cast_id": cast.get("cast_id"),
            "role": cast.get("character"),
            "credit_id": cast.get("credit_id"),
            "order": cast.get("order"),
            "profile": TMDB_PEOPLE_PROFILE_URL % cast.get('id')
        } for cast in casts or []]

    def get_tmdb_directors_actors(self, tmdbinfo):
        """
        ?????????????????????
        :param tmdbinfo: TMDB?????????
        :return: ???????????????????????????
        """
        """
        "cast": [
          {
            "adult": false,
            "gender": 2,
            "id": 3131,
            "known_for_department": "Acting",
            "name": "Antonio Banderas",
            "original_name": "Antonio Banderas",
            "popularity": 60.896,
            "profile_path": "/iWIUEwgn2KW50MssR7tdPeFoRGW.jpg",
            "cast_id": 2,
            "character": "Puss in Boots (voice)",
            "credit_id": "6052480e197de4006bb47b9a",
            "order": 0
          }
        ],
        "crew": [
          {
            "adult": false,
            "gender": 2,
            "id": 5524,
            "known_for_department": "Production",
            "name": "Andrew Adamson",
            "original_name": "Andrew Adamson",
            "popularity": 9.322,
            "profile_path": "/qqIAVKAe5LHRbPyZUlptsqlo4Kb.jpg",
            "credit_id": "63b86b2224b33300a0585bf1",
            "department": "Production",
            "job": "Executive Producer"
          }
        ]
        """
        if not tmdbinfo:
            return [], []
        _credits = tmdbinfo.get("credits")
        if not _credits:
            return [], []
        directors = []
        actors = []
        for cast in self.__dict_media_casts(_credits.get("cast")):
            if cast.get("known_for_department") == "Acting":
                actors.append(cast)
        for crew in self.__dict_media_crews(_credits.get("crew")):
            if crew.get("job") == "Director":
                directors.append(crew)
        return directors, actors

    def get_tmdb_cats(self, mtype, tmdbid):
        """
        ??????TMDB???????????????
        :param: mtype: ????????????
        :param: tmdbid: TMDBID
        """
        try:
            if mtype == MediaType.MOVIE:
                if not self.movie:
                    return []
                return self.__dict_media_casts(self.movie.credits(tmdbid).get("cast"))
            else:
                if not self.tv:
                    return []
                return self.__dict_media_casts(self.tv.credits(tmdbid).get("cast"))
        except Exception as err:
            print(str(err))
        return []

    @staticmethod
    def get_tmdb_genres_names(tmdbinfo):
        """
        ???TMDB???????????????????????????
        """
        """
        "genres": [
            {
              "id": 16,
              "name": "??????"
            },
            {
              "id": 28,
              "name": "??????"
            },
            {
              "id": 12,
              "name": "??????"
            },
            {
              "id": 35,
              "name": "??????"
            },
            {
              "id": 10751,
              "name": "??????"
            },
            {
              "id": 14,
              "name": "??????"
            }
          ]
        """
        if not tmdbinfo:
            return ""
        genres = tmdbinfo.get("genres") or []
        genres_list = [genre.get("name") for genre in genres]
        return ", ".join(genres_list) if genres_list else ""

    @staticmethod
    def get_get_production_country_names(tmdbinfo):
        """
        ???TMDB?????????????????????????????????
        """
        """
        "production_countries": [
            {
              "iso_3166_1": "US",
              "name": "??????"
            }
          ]
        """
        if not tmdbinfo:
            return ""
        countries = tmdbinfo.get("production_countries") or []
        countries_list = [country.get("name") for country in countries]
        return ", ".join(countries_list) if countries_list else ""

    @staticmethod
    def get_tmdb_production_company_names(tmdbinfo):
        """
        ???TMDB?????????????????????????????????
        """
        """
        "production_companies": [
            {
              "id": 2,
              "logo_path": "/wdrCwmRnLFJhEoH8GSfymY85KHT.png",
              "name": "DreamWorks Animation",
              "origin_country": "US"
            }
          ]
        """
        if not tmdbinfo:
            return ""
        companies = tmdbinfo.get("production_companies") or []
        companies_list = [company.get("name") for company in companies]
        return ", ".join(companies_list) if companies_list else ""

    @staticmethod
    def get_tmdb_crews(tmdbinfo, nums=None):
        """
        ???TMDB???????????????????????????
        """
        if not tmdbinfo:
            return ""
        crews = tmdbinfo.get("credits", {}).get("crew") or []
        result = [{crew.get("name"): crew.get("job")} for crew in crews]
        if nums:
            return result[:nums]
        else:
            return result

    def get_tmdb_en_title(self, media_info):
        """
        ??????TMDB???????????????
        """
        en_info = self.get_tmdb_info(mtype=media_info.type,
                                     tmdbid=media_info.tmdb_id,
                                     language="en-US")
        if en_info:
            return en_info.get("title") if media_info.type == MediaType.MOVIE else en_info.get("name")
        return None

    def get_episode_title(self, media_info):
        """
        ?????????????????????
        """
        if media_info.type == MediaType.MOVIE:
            return None
        if media_info.tmdb_id:
            if not media_info.begin_episode:
                return None
            episodes = self.get_tmdb_season_episodes(tmdbid=media_info.tmdb_id,
                                                     season=int(media_info.get_season_seq()))
            for episode in episodes:
                if episode.get("episode_number") == media_info.begin_episode:
                    return episode.get("name")
        return None

    def get_movie_discover(self, page=1):
        """
        ????????????
        """
        if not self.movie:
            return []
        try:
            movies = self.movie.discover(page)
            if movies:
                return movies.get("results")
        except Exception as e:
            print(str(e))
        return []

    def get_movie_similar(self, tmdbid, page=1):
        """
        ??????????????????
        """
        if not self.movie:
            return []
        try:
            movies = self.movie.similar(movie_id=tmdbid, page=page) or []
            return self.__dict_movieinfos(movies)
        except Exception as e:
            print(str(e))
            return []

    def get_movie_recommendations(self, tmdbid, page=1):
        """
        ????????????????????????
        """
        if not self.movie:
            return []
        try:
            movies = self.movie.recommendations(movie_id=tmdbid, page=page) or []
            return self.__dict_movieinfos(movies)
        except Exception as e:
            print(str(e))
            return []

    def get_tv_similar(self, tmdbid, page=1):
        """
        ?????????????????????
        """
        if not self.tv:
            return []
        try:
            tvs = self.tv.similar(tv_id=tmdbid, page=page) or []
            return self.__dict_tvinfos(tvs)
        except Exception as e:
            print(str(e))
            return []

    def get_tv_recommendations(self, tmdbid, page=1):
        """
        ???????????????????????????
        """
        if not self.tv:
            return []
        try:
            tvs = self.tv.recommendations(tv_id=tmdbid, page=page) or []
            return self.__dict_tvinfos(tvs)
        except Exception as e:
            print(str(e))
            return []

    def get_person_medias(self, personid, mtype, page=1):
        """
        ??????????????????????????????
        """
        if not self.person:
            return []
        result = []
        try:
            if mtype == MediaType.MOVIE:
                movies = self.person.movie_credits(person_id=personid) or []
                result = self.__dict_movieinfos(movies)
            elif mtype == MediaType.TV:
                tvs = self.person.tv_credits(person_id=personid) or []
                result = self.__dict_tvinfos(tvs)
            return result[(page - 1) * 20: page * 20]
        except Exception as e:
            print(str(e))
        return []

    @staticmethod
    def __search_engine(feature_name):
        """
        ?????????????????????
        """
        is_movie = False
        if not feature_name:
            return None, is_movie
        # ?????????????????????
        feature_name = re.compile(r"^\w+??????[??????]?", re.IGNORECASE).sub("", feature_name)
        backlist = sorted(KEYWORD_BLACKLIST, key=lambda x: len(x), reverse=True)
        for single in backlist:
            feature_name = feature_name.replace(single, " ")
        if not feature_name:
            return None, is_movie

        def cal_score(strongs, r_dict):
            for i, s in enumerate(strongs):
                if len(strongs) < 5:
                    if i < 2:
                        score = KEYWORD_SEARCH_WEIGHT_3[0]
                    else:
                        score = KEYWORD_SEARCH_WEIGHT_3[1]
                elif len(strongs) < 10:
                    if i < 2:
                        score = KEYWORD_SEARCH_WEIGHT_2[0]
                    else:
                        score = KEYWORD_SEARCH_WEIGHT_2[1] if i < (len(strongs) >> 1) else KEYWORD_SEARCH_WEIGHT_2[2]
                else:
                    if i < 2:
                        score = KEYWORD_SEARCH_WEIGHT_1[0]
                    else:
                        score = KEYWORD_SEARCH_WEIGHT_1[1] if i < (len(strongs) >> 2) else KEYWORD_SEARCH_WEIGHT_1[
                            2] if i < (
                                len(strongs) >> 1) \
                            else KEYWORD_SEARCH_WEIGHT_1[3] if i < (len(strongs) >> 2 + len(strongs) >> 1) else \
                            KEYWORD_SEARCH_WEIGHT_1[
                                4]
                if r_dict.__contains__(s.lower()):
                    r_dict[s.lower()] += score
                    continue
                r_dict[s.lower()] = score

        bing_url = "https://www.cn.bing.com/search?q=%s&qs=n&form=QBRE&sp=-1" % feature_name
        baidu_url = "https://www.baidu.com/s?ie=utf-8&tn=baiduhome_pg&wd=%s" % feature_name
        res_bing = RequestUtils(timeout=5).get_res(url=bing_url)
        res_baidu = RequestUtils(timeout=5).get_res(url=baidu_url)
        ret_dict = {}
        if res_bing and res_bing.status_code == 200:
            html_text = res_bing.text
            if html_text:
                html = etree.HTML(html_text)
                strongs_bing = list(
                    filter(lambda x: (0 if not x else difflib.SequenceMatcher(None, feature_name,
                                                                              x).ratio()) > KEYWORD_STR_SIMILARITY_THRESHOLD,
                           map(lambda x: x.text, html.cssselect(
                               "#sp_requery strong, #sp_recourse strong, #tile_link_cn strong, .b_ad .ad_esltitle~div strong, h2 strong, .b_caption p strong, .b_snippetBigText strong, .recommendationsTableTitle+.b_slideexp strong, .recommendationsTableTitle+table strong, .recommendationsTableTitle+ul strong, .pageRecoContainer .b_module_expansion_control strong, .pageRecoContainer .b_title>strong, .b_rs strong, .b_rrsr strong, #dict_ans strong, .b_listnav>.b_ans_stamp>strong, #b_content #ans_nws .na_cnt strong, .adltwrnmsg strong"))))
                if strongs_bing:
                    title = html.xpath("//aside//h2[@class = \" b_entityTitle\"]/text()")
                    if len(title) > 0:
                        if title:
                            t = re.compile(r"\s*\(\d{4}\)$").sub("", title[0])
                            ret_dict[t] = 200
                            if html.xpath("//aside//div[@data-feedbk-ids = \"Movie\"]"):
                                is_movie = True
                    cal_score(strongs_bing, ret_dict)
        if res_baidu and res_baidu.status_code == 200:
            html_text = res_baidu.text
            if html_text:
                html = etree.HTML(html_text)
                ems = list(
                    filter(lambda x: (0 if not x else difflib.SequenceMatcher(None, feature_name,
                                                                              x).ratio()) > KEYWORD_STR_SIMILARITY_THRESHOLD,
                           map(lambda x: x.text, html.cssselect("em"))))
                if len(ems) > 0:
                    cal_score(ems, ret_dict)
        if not ret_dict:
            return None, False
        ret = sorted(ret_dict.items(), key=lambda d: d[1], reverse=True)
        log.info("???Meta????????????????????????%s ..." % ([k[0] for i, k in enumerate(ret) if i < 4]))
        if len(ret) == 1:
            keyword = ret[0][0]
        else:
            pre = ret[0]
            nextw = ret[1]
            if nextw[0].find(pre[0]) > -1:
                # ??????????????????
                if int(pre[1]) >= 100:
                    keyword = pre[0]
                # ????????????30 ????????? ?????????
                elif int(pre[1]) - int(nextw[1]) > KEYWORD_DIFF_SCORE_THRESHOLD:
                    keyword = pre[0]
                # ???????????????
                elif nextw[0].replace(pre[0], "").strip() == pre[0]:
                    keyword = pre[0]
                # ???????????????
                elif pre[0].isdigit():
                    keyword = nextw[0]
                else:
                    keyword = nextw[0]

            else:
                keyword = pre[0]
        log.info("???Meta????????????????????????%s " % keyword)
        return keyword, is_movie

    @staticmethod
    def __get_genre_ids_from_detail(genres):
        """
        ???TMDB???????????????genre_id??????
        """
        if not genres:
            return []
        genre_ids = []
        for genre in genres:
            genre_ids.append(genre.get('id'))
        return genre_ids

    @staticmethod
    def __get_tmdb_chinese_title(tmdbinfo):
        """
        ??????????????????????????????
        """
        if not tmdbinfo:
            return None
        if tmdbinfo.get("media_type") == MediaType.MOVIE:
            alternative_titles = tmdbinfo.get("alternative_titles", {}).get("titles", [])
        else:
            alternative_titles = tmdbinfo.get("alternative_titles", {}).get("results", [])
        for alternative_title in alternative_titles:
            iso_3166_1 = alternative_title.get("iso_3166_1")
            if iso_3166_1 == "CN":
                title = alternative_title.get("title")
                if title and StringUtils.is_chinese(title) and zhconv.convert(title, "zh-hans") == title:
                    return title
        return tmdbinfo.get("title") if tmdbinfo.get("media_type") == MediaType.MOVIE else tmdbinfo.get("name")

    def get_tmdbperson_chinese_name(self, person_id):
        """
        ??????TMDB??????????????????
        """
        if not self.person:
            return ""
        alter_names = []
        name = ""
        try:
            aka_names = self.person.details(person_id).get("also_known_as", []) or []
        except Exception as err:
            print(str(err))
            return ""
        for aka_name in aka_names:
            if StringUtils.is_chinese(aka_name):
                alter_names.append(aka_name)
        if len(alter_names) == 1:
            name = alter_names[0]
        elif len(alter_names) > 1:
            for alter_name in alter_names:
                if alter_name == zhconv.convert(alter_name, 'zh-hans'):
                    name = alter_name
        return name

    def get_tmdbperson_aka_names(self, person_id):
        """
        ??????????????????
        """
        if not self.person:
            return []
        try:
            aka_names = self.person.details(person_id).get("also_known_as", []) or []
            return aka_names
        except Exception as err:
            print(str(err))
            return []

    def get_random_discover_backdrop(self):
        """
        ??????TMDB?????????????????????????????????
        """
        movies = self.get_movie_discover()
        if movies:
            backdrops = [movie.get("backdrop_path") for movie in movies]
            return TMDB_IMAGE_ORIGINAL_URL % backdrops[round(random.uniform(0, len(backdrops) - 1))]
        return ""

    def save_rename_cache(self, file_name, cache_info):
        """
        ????????????????????????????????????
        """
        if not file_name or not cache_info:
            return
        meta_info = MetaInfo(title=file_name)
        self.__insert_media_cache(self.__make_cache_key(meta_info), cache_info)

    @staticmethod
    def merge_media_info(target, source):
        """
        ???soruce???????????????????????????target????????????
        """
        target.set_tmdb_info(source.tmdb_info)
        target.fanart_poster = source.get_poster_image()
        target.fanart_backdrop = source.get_backdrop_image()
        target.set_download_info(download_setting=source.download_setting,
                                 save_path=source.save_path)
        return target

    def get_tmdbid_by_imdbid(self, imdbid):
        """
        ??????IMDBID??????TMDB??????
        """
        if not self.find:
            return None
        try:
            result = self.find.find_by_imdbid(imdbid) or {}
            tmdbinfo = result.get('movie_results') or result.get("tv_results")
            if tmdbinfo:
                tmdbinfo = tmdbinfo[0]
                return tmdbinfo.get("id")
        except Exception as err:
            print(str(err))
        return None

    @staticmethod
    def get_intersection_episodes(target, source, title):
        """
        ?????????????????????????????????????????????????????????????????????
        """
        if not source or not title:
            return target
        if not source.get(title):
            return target
        if not target.get(title):
            target[title] = source.get(title)
            return target
        index = -1
        for target_info in target.get(title):
            index += 1
            source_info = None
            for info in source.get(title):
                if info.get("season") == target_info.get("season"):
                    source_info = info
                    break
            if not source_info:
                continue
            if not source_info.get("episodes"):
                continue
            if not target_info.get("episodes"):
                target_episodes = source_info.get("episodes")
                target[title][index]["episodes"] = target_episodes
                continue
            target_episodes = list(set(target_info.get("episodes")).intersection(set(source_info.get("episodes"))))
            target[title][index]["episodes"] = target_episodes
        return target

    @staticmethod
    def get_detail_url(mtype, tmdbid):
        """
        ??????TMDB/?????????????????????
        """
        if not tmdbid:
            return ""
        if str(tmdbid).startswith("DB:"):
            return "https://movie.douban.com/subject/%s" % str(tmdbid).replace("DB:", "")
        elif mtype == MediaType.MOVIE:
            return "https://www.themoviedb.org/movie/%s" % tmdbid
        else:
            return "https://www.themoviedb.org/tv/%s" % tmdbid

    def get_episode_images(self, tv_id, season_id, episode_id):
        """
        ??????????????????????????????
        """
        res = self.tv.episode_images(tv_id, season_id, episode_id)
        if len(res.get("stills", [])) > 0:
            return TMDB_IMAGE_W500_URL % res.get("stills", [{}])[0].get("file_path")
        else:
            return ""

    def get_tmdb_factinfo(self, media_info):
        """
        ??????TMDB????????????
        """
        result = []
        if media_info.vote_average:
            result.append({"??????": media_info.vote_average})
        if media_info.original_title:
            result.append({"????????????": media_info.original_title})
        status = media_info.tmdb_info.get("status")
        if status:
            result.append({"??????": status})
        if media_info.release_date:
            result.append({"????????????": media_info.release_date})
        revenue = media_info.tmdb_info.get("revenue")
        if revenue:
            result.append({"??????": StringUtils.str_amount(revenue)})
        budget = media_info.tmdb_info.get("budget")
        if media_info.vote_average:
            result.append({"??????": StringUtils.str_amount(budget)})
        if budget:
            result.append({"????????????": media_info.original_language})
        production_country = self.get_get_production_country_names(tmdbinfo=media_info.tmdb_info)
        if production_country:
            result.append({"????????????": production_country}),
        production_company = self.get_tmdb_production_company_names(tmdbinfo=media_info.tmdb_info)
        if production_company:
            result.append({"????????????": production_company})

        return result
