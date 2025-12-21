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

import { BaseConstants, BreakpointConstants, StandardIcon } from '@ohos/base';
import { HomeConstants } from '../../constants/HomeConstants';
import { deviceInfo } from '@kit.BasicServicesKit';

@Component
export struct ChatDetailTopSearch {
  @StorageProp('currentBreakpoint') currentBreakpoint: string = BreakpointConstants.BREAKPOINT_SM;
  @Consume('pageInfo') pageInfo: NavPathStack;
  private currentChatUserName: string = "";

  build() {
    Flex({ alignItems: ItemAlign.Center, justifyContent: FlexAlign.SpaceBetween }) {
      StandardIcon({ icon: $r('app.media.ic_public_back') })
        .onClick(() => {
          if (this.currentBreakpoint === BreakpointConstants.BREAKPOINT_SM) {
            this.pageInfo.clear();
          } else if (this.pageInfo.size() === 1) {
            this.pageInfo.clear();
            this.pageInfo.pushPath({ name: 'ChatDetailNone' });
          } else {
            this.pageInfo.clear();
            this.pageInfo.pushPath({ name: 'ChatDetailNone' });
          }
        })
      Text(this.currentChatUserName || HomeConstants.CHAT_DETAIL_TOP_TITLE)
        .fontWeight(BaseConstants.FONT_WEIGHT_FIVE)
        .fontSize(BaseConstants.FONT_SIZE_EIGHTEEN)
        .fontFamily(BaseConstants.FONT_FAMILY_MEDIUM)
        .fontColor($r('app.color.chat_default_text_color'))
      StandardIcon({ icon: $r('app.media.ic_public_more') })
    }
    .width(BaseConstants.FULL_WIDTH)
    .height(BaseConstants.FULL_HEIGHT)
    .padding({
      left: $r('app.float.icon_margin_two'),
      right: $r('app.float.icon_margin_two'),
      top: deviceInfo.deviceType === BaseConstants.DEVICE_2IN1 ? $r('app.float.zero') :
      $r('app.float.device_padding_top')
    })
  }
}