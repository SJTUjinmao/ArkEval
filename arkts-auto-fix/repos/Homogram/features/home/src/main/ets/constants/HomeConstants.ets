/*
 * Copyright (c) 2023 Huawei Device Co., Ltd.
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */
import { SymbolGlyphModifier } from '@kit.ArkUI';
import { BaseConstants } from '@ohos/base';

export class HomeConstants {
  /**
   * Grid row column list.
   */
  static readonly GRID_ROW_COLUMNS: number[] = [1, 2, 3, 4, 5];
  static readonly GRID_COL_COLUMNS: number[] = [0, 1, 2, 3, 4, 5];
  static readonly TAB_MESSAGE_INDEX = 0;
  /**
   * Index of CONTACTS tab.
   */
  static readonly TAB_CONTACTS_INDEX = 1;
  /**
   * Index of SOCIAL_CIRCLE tab.
   */
  static readonly TAB_SOCIAL_CIRCLE_INDEX = 2;
  /**
   * Index of ME tab.
   */
  static readonly TAB_ME_INDEX = 3;
  static readonly CONTACTS_DETAIL_AVATAR_Z_INDEX = 2;
  /**
   * Image message
   */
  static readonly USER_PHONE = '15988880000';
  static readonly LAST_POST = '最新动态';
  static readonly IMAGES_SCOPE = 2;
  static readonly IMAGE_ASPECT_RATIO = 1;
  /**
   * Contacts list detail
   */
  static readonly CONTACTS_NAME = '通讯录';
  static readonly CONTACTS_LIST = ['群组', '名片夹'];
  static readonly CONTACTS_DEFAULT_SELECTED_INDEX = 0;
  static readonly CONTACTS_DEFAULT_SELECTED_NAME = '阿兰';
  /**
   * Chat detail
   */
  static readonly CHAT_LIST_APPLET =
    ['芬芬邀请你喝奶茶啦', '请你喝奶茶，快点单吧！！速度速度，就差你了', '哈哈，今天有什么喜事说来听听'];
  static readonly CHAT_LIST_DOCUMENT =
    ['市场部2023年新产品宣传规划', '这是明天会议的资料，请发给Jack，让他确认', '好的，收到'];
  static readonly IS_APPLET_CHAT = true;
  static readonly IS_DOCUMENT_CHAT = true;
  static readonly IS_RECEIVED = true;
  static readonly CHAT_DEFAULT_SIZE = 3;
  static readonly CHAT_TITLE = '消息';
  /**
   * Chat top search
   */
  static readonly SEARCH_TEXT_FLEX_BASIS = 150;
  static readonly SEARCH_TEXT_INPUT = '搜索...';
  static readonly ENABLE_KEY_BOARD_ON_FOCUS = false;
  static readonly FLEX_BASIS_AUTO = 'auto';
  /**
   * Chat list
   */
  static readonly CHAT_LIST_NAME_MAX_LINE = 1;
  /**
   * Chat detail
   */
  static readonly CHAT_DETAIL_BOTTOM_TEXT_RADIUS = 20;
  static readonly CHAT_DETAIL_TOP_TITLE = '芬芬';
  static readonly CHAT_DETAIL_MAX_LINE = 1;
  static readonly CHAT_DETAIL_FILL_OPACITY = 1;
  static readonly CHAT_DETAIL_STROKE_WIDTH = 1;
  static readonly PATH_BORDER_RADIUS = 16;
  /**
   * Applet detail
   */
  static readonly APPLET_TITLE = '奶茶';
  static readonly APPLET_DESCRIBE = '应用';
  /**
   * Document detail
   */
  static readonly DOCUMENT_SIZE = '24KB';
  static readonly DOCUMENT_INTRODUCE = '文档';
  static readonly DOCUMENT_DIVIDER_WIDTH = 1;
  /**
   * Default avatar
   */
  static readonly DEFAULT_AVATAR = $r('app.media.avatar1')
  /**
   * Navigation navBarWidth percent
   */
  static readonly NAVIGATION_NAV_BAR_WIDTH_MD = '50%'
  static readonly NAVIGATION_NAV_BAR_WIDTH_LG = '44.5%'
  /**
   * Home Tab Bar icons and texts
   */
  static readonly TAB_BAR_ICONS = [
    new SymbolGlyphModifier($r('sys.symbol.message_on_message')),
    new SymbolGlyphModifier($r('sys.symbol.person_2')),
    new SymbolGlyphModifier($r('sys.symbol.person')),
  ];
  static readonly TAB_BAR_SELECTED_ICONS = [
    new SymbolGlyphModifier($r('sys.symbol.message_on_message_fill')),
    new SymbolGlyphModifier($r('sys.symbol.person_2_fill')),
    new SymbolGlyphModifier($r('sys.symbol.person_fill')),
  ];
  static readonly TAB_BAR_TEXTS = [
    $r('app.string.message'),
    $r('app.string.contacts'),
    $r('app.string.me'),
  ];
  static readonly TAB_BAR_NEW_MESSAGE_ICON = new SymbolGlyphModifier($r('sys.symbol.paintbrush_pointed_circle_fill'));
  static readonly TAB_BAR_PADDING = 10;
  static readonly CHAT_ITEM_PROFILE_PHOTO_SIZE = 48;
  static readonly CHAT_ITEM_PROFILE_PHOTO_MARGIN_LEFT = 24;
  static readonly CHAT_ITEM_PROFILE_PHOTO_MARGIN_RIGHT = 12;
  static readonly CHAT_LIST_MAIN_COLOR = $r('sys.color.black');
  static readonly CHAT_LIST_SCROLLER = "chatListScroller";
  static readonly CHAT_LIST_DIVIDER_START_MARGIN =
    HomeConstants.CHAT_ITEM_PROFILE_PHOTO_SIZE + HomeConstants.CHAT_ITEM_PROFILE_PHOTO_MARGIN_LEFT +
    HomeConstants.CHAT_ITEM_PROFILE_PHOTO_MARGIN_RIGHT;
  static readonly CHAT_LIST_DIVIDER_END_MARGIN = 12;
  static readonly NAV_SIGNAL_BAR_HEIGHT = 36;
  static readonly NAV_TITLE_FONT_SIZE = 18;
  static readonly NAV_TITLE_FONT_SIZE_LARGE = 26;
  static readonly NAV_TITLE_TOP_MARGIN = 36;
  static readonly NAV_TITLE_LEFT_MARGIN = 24;
  static readonly NAV_TITLE_TOP_MARGIN_MINI = 56;
  static readonly NAV_MENU_TOP_MARGIN = HomeConstants.NAV_TITLE_TOP_MARGIN;
  static readonly CHAT_LIST_ITEM_MARGIN = 5;
  static readonly CHAT_LIST_SEARCH_BAR_HEIGHT = 40;
  static readonly CHAT_LIST_SEARCH_BAR_WIDTH = '95%';
  static readonly CHAT_LIST_SEARCH_BAR_PADDING = 10;
  static readonly CHAT_LIST_SEARCH_BAR_FONT_SIZE = BaseConstants.FONT_SIZE_SIXTEEN;
  static readonly CHAT_LIST_SEARCH_BAR_MARGIN = 10;
  static readonly CHAT_DETAIL_MARGIN_TOP = HomeConstants.NAV_MENU_TOP_MARGIN + HomeConstants.NAV_SIGNAL_BAR_HEIGHT;
  static readonly MESSAGE_BACKGROUND_COLOR = $r("app.color.message_background_color");
  static readonly MESSAGE_BACKGROUND_COLOR_OUTGOING = $r("app.color.message_background_color_outgoing");
  static readonly MESSAGE_REPLY_TO_PREVIEW_BACKGROUND_COLOR = $r("app.color.message_reply_to_preview_background_color");
  static readonly MESSAGE_REPLY_TO_PREVIEW_DIVIDER_COLOR = $r("app.color.message_reply_to_preview_divider_color");
  static readonly MESSAGE_REPLY_TO_PREVIEW_DIVIDER_WIDTH = 4;
  static readonly MESSAGE_REPLY_TO_PREVIEW_TOP_MARGIN = 8;
  static readonly MESSAGE_TIME_COLOR = $r("app.color.message_time_color");
  static readonly PROFILE_PHOTO_SIZE = 48;
  static readonly FORUM_TOPIC_LIST_PHOTO_SIZE = '96vp';
  static readonly PROFILE_PHOTO_SIZE_SMALL = '40vp';
}