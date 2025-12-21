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

import { BaseConstants, BreakpointConstants, CircleButton, StandardIcon } from '@ohos/base';
import { HomeConstants } from '../../constants/HomeConstants';
import { deviceInfo } from '@kit.BasicServicesKit';

@Preview
@Component
export struct HomeTopSearch {
  @StorageProp('currentBreakpoint') currentBreakpoint: string = BreakpointConstants.BREAKPOINT_SM;
  @Prop title: string | Resource = "Message";
  @Prop loading: boolean = false;

  build() {
    Flex({ alignItems: ItemAlign.Center, justifyContent: FlexAlign.SpaceBetween }) {
      Column() {
        Text(this.title)
          .fontWeight(BaseConstants.FONT_WEIGHT_FIVE)
          .fontSize(BaseConstants.FONT_SIZE_TWENTY_FOUR)
          .fontFamily(BaseConstants.FONT_FAMILY_MEDIUM)
          .width(BaseConstants.FULL_WIDTH)
      }
      .flexBasis(HomeConstants.SEARCH_TEXT_FLEX_BASIS)

      if (this.currentBreakpoint === BreakpointConstants.BREAKPOINT_LG) {
        Column() {
          this.searchBar()
        }
        .flexBasis(HomeConstants.FLEX_BASIS_AUTO)
        .margin({ right: $r('app.float.icon_margin_two') })
      } else {
        Blank()
        StandardIcon({ icon: $r('app.media.ic_public_search') })
          .margin({ right: $r('app.float.phone_icon_right') })
      }
      Column() {
        // StandardIcon({ icon: $r('app.media.ic_public_add_norm') })
        CircleButton({icon: $r('sys.symbol.search_things')})
          .flexBasis(HomeConstants.FLEX_BASIS_AUTO)
      }
      .margin({ right: $r('app.float.phone_icon_right') })
    }
    .expandSafeArea([SafeAreaType.SYSTEM])
    .width(BaseConstants.FULL_WIDTH)
    .backgroundBlurStyle(BlurStyle.Thin)
    .backgroundColor(Color.Transparent)
    .padding({ left: $r('app.float.phone_icon_left'),top: deviceInfo.deviceType === BaseConstants.DEVICE_2IN1 ? $r('app.float.zero') : $r('app.float.device_padding_top') })
  }

  @Builder
  searchBar() {
    Row() {
      Stack({ alignContent: Alignment.Start }) {
        TextInput({ placeholder: HomeConstants.SEARCH_TEXT_INPUT })
          .placeholderFont({
            size: $r('app.float.text_input_default_font'),
            weight: FontWeight.Normal
          })
          .placeholderColor($r('app.color.text_input_default_color'))
          .height($r('app.float.text_input_height'))
          .fontSize(BaseConstants.FONT_SIZE_SIXTEEN)
          .padding({
            left: $r('app.float.text_input_left'),
            right: $r('app.float.text_input_right')
          })
          .width(BaseConstants.FULL_WIDTH)
          .enableKeyboardOnFocus(HomeConstants.ENABLE_KEY_BOARD_ON_FOCUS)
          .backgroundColor($r('app.color.text_input_default_background_color'))
          .caretColor($r('app.color.text_input_default_care_color'))

        Image($r('app.media.ic_public_search'))
          .width($r('app.float.small_icon_width'))
          .height($r('app.float.small_icon_height'))
          .margin({ left: $r('app.float.add_icon_left') })
      }
      .alignSelf(ItemAlign.Center)
      .layoutWeight(BaseConstants.LAYOUT_WEIGHT_ONE)
    }
    .justifyContent(FlexAlign.Center)
    .width(BaseConstants.FULL_WIDTH)
    // .height(BaseConstants.FULL_HEIGHT)
  }
}