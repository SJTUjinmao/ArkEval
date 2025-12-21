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

import { BaseConstants as Constants } from '../constants/BaseConstants';
import { BreakpointType } from '../utils/Breakpoint/BreakpointType';

export class Adaptive {
  static CircleImageOneWidth = (currentBreakpoint: string): string => {
    return new BreakpointType(
      Constants.IMAGE_ONE_WIDTH_SM,
      Constants.IMAGE_ONE_WIDTH_MD,
      Constants.IMAGE_ONE_WIDTH_LG
    ).GetValue(currentBreakpoint);
  }
  static HomeTabHeight = (currentBreakpoint: string): string => {
    return new BreakpointType(
      Constants.HOME_TAB_HEIGHT_SM,
      Constants.HOME_TAB_HEIGHT_MD,
      Constants.HOME_TAB_HEIGHT_LG
    ).GetValue(currentBreakpoint);
  }
  static HomeTabWidth = (currentBreakpoint: string): string => {
    return new BreakpointType(
      Constants.HOME_TAB_WIDTH_SM,
      Constants.HOME_TAB_WIDTH_MD,
      Constants.HOME_TAB_WIDTH_LG
    ).GetValue(currentBreakpoint);
  }
  static ContactPhoneWidth = (currentBreakpoint: string): string => {
    return new BreakpointType(
      Constants.CONTACTS_PHONE_WIDTH_SM,
      Constants.CONTACTS_PHONE_WIDTH_MD,
      Constants.CONTACTS_PHONE_WIDTH_LG
    ).GetValue(currentBreakpoint);
  }
  static ContactDetailHeight = (currentBreakpoint: string): string => {
    return new BreakpointType(
      Constants.CONTACTS_DETAIL_HEIGHT_SM,
      Constants.CONTACTS_DETAIL_HEIGHT_MD,
      Constants.CONTACTS_DETAIL_HEIGHT_LG
    ).GetValue(currentBreakpoint);
  }
  static ContactItemHeight = (currentBreakpoint: string): string => {
    return new BreakpointType(
      Constants.CONTACTS_ITEM_HEIGHT_SM,
      Constants.CONTACTS_ITEM_HEIGHT_MD,
      Constants.CONTACTS_ITEM_HEIGHT_LG
    ).GetValue(currentBreakpoint);
  }
  static DocumentTitleColumnHeight = (currentBreakpoint: string): string => {
    return new BreakpointType(
      Constants.DOCUMENT_TITLE_HEIGHT_SM,
      Constants.DOCUMENT_TITLE_HEIGHT_MD,
      Constants.DOCUMENT_TITLE_HEIGHT_LG
    ).GetValue(currentBreakpoint);
  }
  static DocumentTitleColumnSpace = (currentBreakpoint: string): number => {
    return new BreakpointType(
      Constants.DOCUMENT_TITLE_SPACE_SM,
      Constants.DOCUMENT_TITLE_SPACE_MD,
      Constants.DOCUMENT_TITLE_SPACE_LG
    ).GetValue(currentBreakpoint);
  }
}