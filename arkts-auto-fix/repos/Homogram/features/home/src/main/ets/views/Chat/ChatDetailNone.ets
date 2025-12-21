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

import { BaseConstants } from '@ohos/base/src/main/ets/constants/BaseConstants';
import { deviceInfo } from '@kit.BasicServicesKit';

@Component
export default struct ChatDetailNone {
  build() {
    NavDestination() {
      Flex({ justifyContent: FlexAlign.Center, alignItems: ItemAlign.Center }) {
        Image($r('app.media.ic_none_message'))
          .width($r('app.float.chat_none_icon_width'))
          .height($r('app.float.chat_none_icon_height'))
      }
      .backgroundColor($r('app.color.chat_none_icon_bg_color'))
      .height(BaseConstants.FULL_HEIGHT)
    }
    .padding({
      top: deviceInfo.deviceType === BaseConstants.DEVICE_2IN1 ? $r('app.float.zero') :
      $r('app.float.device_padding_top')
    })
    .hideTitleBar(true)
  }
}