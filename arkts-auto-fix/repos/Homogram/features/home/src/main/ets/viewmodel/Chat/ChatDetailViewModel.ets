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

import { Want, common } from '@kit.AbilityKit';
import { BusinessError } from '@kit.BasicServicesKit';
import { hilog } from '@kit.PerformanceAnalysisKit';
import { Logger } from '@ohos/base';

class ChatViewModel {
  public startAppletAbility(context: common.UIAbilityContext, index: number): void {
    const want: Want = {
      bundleName: getContext(context).applicationInfo.name,
      abilityName: 'AppletAbility',
      parameters: {
        position: index
      }
    };
    try {
      context.startAbility(want, (err: BusinessError) => {
        if (err.code) {
          Logger.error(`startAbility failed, code is ${err.code}, message is ${err.message}`);
        }
      });
    } catch (error) {
      hilog.error(0x00004, 'ChatViewModel', '%{public}s', error);
    }
  }

  public startDocumentAbility(context: common.UIAbilityContext, index: number): void {
    const want: Want = {
      bundleName: getContext(context).applicationInfo.name,
      abilityName: 'DocumentAbility',
      parameters: {
        position: index
      }
    };
    try {
      context.startAbility(want, (err: BusinessError) => {
        if (err.code) {
          Logger.error(`startAbility failed, code is ${err.code}, message is ${err.message}`);
        }
      });
    } catch (error) {
      Logger.error(`startAbility failed,  message is ${error}`);
    }
  }
}

export { ChatViewModel };