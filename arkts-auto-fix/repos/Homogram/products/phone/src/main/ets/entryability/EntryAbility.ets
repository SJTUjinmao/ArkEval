/*
 * Copyright (c) 2022 Huawei Device Co., Ltd.
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

import { ConfigurationConstant, UIAbility } from '@kit.AbilityKit';
import { hilog } from '@kit.PerformanceAnalysisKit';
import { display, KeyboardAvoidMode, window } from '@kit.ArkUI';
import { AsyncCallback, BusinessError, commonEventManager } from '@kit.BasicServicesKit';
import { BaseConstants, BreakpointConstants, Logger, KV, Database, delay, WindowUtil } from '@ohos/base/Index';
import { deviceInfo } from '@kit.BasicServicesKit';
import { registerDevice, run, stop, reconnect, EventConstants } from '@ohos/home'
import { pushService } from '@kit.PushKit';
import { emitter } from '@kit.BasicServicesKit';
import AppDB from '../AppDB';
import { ImageKnife } from '@ohos/imageknife';

const PREF_STORE_NAME = 'tg_preferences';
const KEY_CURRENT_SESSION = 'tg_current_session';

export default class EntryAbility extends UIAbility {
  private windowObj?: window.Window;
  private KV: KV | null = null;
  private isFirstOnForeground: boolean = true;
  private subscriber: commonEventManager.CommonEventSubscriber | null = null;
  //订阅者信息
  private subscribeInfo: commonEventManager.CommonEventSubscribeInfo = {
    events: [
      commonEventManager.Support.COMMON_EVENT_MY_PACKAGE_UNSUSPENDED,
      commonEventManager.Support.COMMON_EVENT_MY_PACKAGE_SUSPENDED,
      commonEventManager.Support.COMMON_EVENT_PACKAGES_SUSPENDED,
    // commonEventManager.Support.COMMON_EVENT_CONNECTIVITY_CHANGE,
    // commonEventManager.Support.COMMON_EVENT_NETWORK_STATE_CHANGED
    ]
  };

  onCreate() {
    // this.KV = new KV(this.context, PREF_STORE_NAME);
    // AppStorage.setOrCreate('KV', this.KV);
    // Database.deleteRdb(this.context);
    this.context.getApplicationContext().setColorMode(ConfigurationConstant.ColorMode.COLOR_MODE_NOT_SET);
    ImageKnife.getInstance().initFileCache(this.context, 256, 256 * 1024 * 1024);
    try {
      AppDB.initDb(this.context);
      Logger.info('Database initialized successfully');
    } catch (err) {
      Logger.error('Failed to init database. Cause: ' + err.name + "-" + err.message);
    }
    pushService.getToken().then((token: string) => {
      registerDevice(token).then((result: boolean) => {
        if (result) {
          Logger.info('Device registered successfully');
        } else {
          Logger.error('Failed to register device but no error');
        }
      }).catch((err: Error) => {
        Logger.error('Failed to register device. Cause: ' + err.name + "-" + err.message);
      });
    }).catch((err: Error) => {
      Logger.error('Failed to get push token. Cause: ' + err.name + "-" + err.message);
    });

    //创建订阅者
    try {
      commonEventManager.createSubscriber(this.subscribeInfo, this.createCB);
      console.log('commonEvent createSubscriber success');
    } catch (error) {
      let err: BusinessError = error as BusinessError;
      console.error(`commonEvent createSubscriber failed, code is ${err.code}, message is ${err.message}`);
    }

    hilog.info(0x0000, 'testTag', '%{public}s', 'Ability onCreate');
  }

  onDestroy(): void {
    hilog.info(0x0000, 'testTag', '%{public}s', 'Ability onDestroy');
  }

  async onWindowStageCreate(windowStage: window.WindowStage): Promise<void> {
    // Main window is created, set main page for this ability
    hilog.info(0x0000, 'testTag', '%{public}s', 'Ability onWindowStageCreate');
    windowStage.getMainWindow((err: BusinessError<void>, data) => {
      if (err.code) {
        hilog.info(0x0000, 'testTag', '%{public}s', 'getMainWindow failed');
        return;
      }
      this.windowObj = data;
      let windowWidth = this.windowObj.getWindowProperties().windowRect.width;
      let windowHeight = this.windowObj.getWindowProperties().windowRect.height;
      this.updateWidthBp(windowWidth);
      this.updateHeightBp(windowWidth, windowHeight);
      this.windowObj.on('windowSizeChange', (windowSize: window.Size) => {
        this.updateWidthBp(windowSize.width);
        this.updateHeightBp(windowSize.width, windowSize.height);
      })
      if (deviceInfo.deviceType !== BaseConstants.DEVICE_2IN1) {
        // data.setWindowLayoutFullScreen(true);
      }
    })
    let windowUtil: WindowUtil | undefined = WindowUtil.getInstance();
    if (windowUtil !== undefined) {
      windowUtil.setWindowStage(windowStage);
      windowUtil.setMainWindowPortrait();
    }

    windowStage.loadContent('pages/Index', (err, data) => {
      if (err.code) {
        hilog.error(0x0000, 'testTag', 'Failed to load the content. Cause: %{public}s', JSON.stringify(err) ?? '');
        return;
      }
      let type = window.AvoidAreaType.TYPE_NAVIGATION_INDICATOR; // 以导航条避让为例
      let avoidArea = this.windowObj?.getWindowAvoidArea(type);
      let bottomRectHeight = avoidArea?.bottomRect.height; // 获取到导航条区域的高度
      AppStorage.setOrCreate('bottomRectHeight', bottomRectHeight);

      type = window.AvoidAreaType.TYPE_SYSTEM; // 以状态栏避让为例
      avoidArea = this.windowObj?.getWindowAvoidArea(type);
      let topRectHeight = avoidArea?.topRect.height; // 获取状态栏区域高度
      AppStorage.setOrCreate('topRectHeight', topRectHeight);

      this.windowObj?.on('avoidAreaChange', (data) => {
        if (data.type === window.AvoidAreaType.TYPE_SYSTEM) {
          let topRectHeight = data.area.topRect.height;
          AppStorage.setOrCreate('topRectHeight', topRectHeight);
        } else if (data.type == window.AvoidAreaType.TYPE_NAVIGATION_INDICATOR) {
          let bottomRectHeight = data.area.bottomRect.height;
          AppStorage.setOrCreate('bottomRectHeight', bottomRectHeight);
        }
      });
      hilog.info(0x0000, 'testTag', 'Succeeded in loading the content. Data: %{public}s', JSON.stringify(data) ?? '');
    });

  }

  onWindowStageDestroy(): void {
    // Main window is destroyed, release UI related resources
    hilog.info(0x0000, 'testTag', '%{public}s', 'Ability onWindowStageDestroy');
  }

  async onForeground(): Promise<void> {
    // Ability has brought to foreground
    hilog.info(0x0000, 'testTag', '%{public}s', 'Ability onForeground');
    if (!this.isFirstOnForeground) {
      // await reconnect();
      // check if recovered from suspend

      // await delay(500); // TODO: wait for everything to be handled?
      emitter.emit(EventConstants.onForegroundEvent);
      // run()
      //   .catch((err: Error) => {
      //     Logger.error('onForeground Failed to homo::tg::run(). Cause: ' + err.name + "-" + err.message);
      //   });
    }
  }

  onBackground(): void {
    // Ability has back to background
    hilog.info(0x0000, 'testTag', '%{public}s', 'Ability onBackground');
    this.isFirstOnForeground = false;
    stop()
      .catch((err: Error) => {
        Logger.error('onBackground Failed to homo::tg::stop(). Cause: ' + err.name + "-" + err.message)
      })
  }

  //订阅公共事件回调
  private subscribeCB: AsyncCallback<commonEventManager.CommonEventData, void> =
    (err: BusinessError, data: commonEventManager.CommonEventData) => {
      if (err) {
        console.error(`commonEvent subscribe failed, code is ${err.code}, message is ${err.message}`);
      } else {
        console.info("commonEvent Consume callback " + JSON.stringify(data));
        console.info("commonEvent subscribe success");
      }
    }
  //创建订阅者回调
  private createCB: AsyncCallback<commonEventManager.CommonEventSubscriber, void> =
    (err: BusinessError, commonEventSubscriber: commonEventManager.CommonEventSubscriber) => {
      if (!err) {
        console.info("commonEvent createSubscriber success");
        this.subscriber = commonEventSubscriber;
        //订阅公共事件
        try {
          commonEventManager.subscribe(this.subscriber, this.subscribeCB);
          console.debug("commonEvent in createCB subscribe success");
        } catch (error) {
          let err: BusinessError = error as BusinessError;
          console.error(`commonEvent subscribe failed, code is ${err.code}, message is ${err.message}`);
        }
      } else {
        console.error(`commonEvent createSubscriber failed, code is ${err.code}, message is ${err.message}`);
      }
    }

  //
  // private updateBreakpoint(windowWidth: number): void {
  //   let windowWidthVp = windowWidth / display.getDefaultDisplaySync().densityPixels;
  //   let curBp: string = '';
  //   if (windowWidthVp < BreakpointConstants.BREAKPOINT_SCOPE[1]) {
  //     curBp = BreakpointConstants.BREAKPOINT_SM;
  //   } else if (windowWidthVp < BreakpointConstants.BREAKPOINT_SCOPE[2]) {
  //     curBp = BreakpointConstants.BREAKPOINT_MD;
  //   } else {
  //     curBp = BreakpointConstants.BREAKPOINT_LG;
  //   }
  //   AppStorage.setOrCreate('currentBreakpoint', curBp);
  // }

  private updateWidthBp(windowWidth: number): void {
    // if (this.windowObj === undefined) {
    //   return;
    // }
    // let windowWidth = this.windowObj.getWindowProperties().windowRect.width;
    let windowWidthVp = windowWidth / display.getDefaultDisplaySync().densityPixels;
    let widthBp: string = '';
    if (windowWidthVp < 320) {
      widthBp = BreakpointConstants.BREAKPOINT_XS;
    } else if (windowWidthVp >= 320 && windowWidthVp < 600) {
      widthBp = BreakpointConstants.BREAKPOINT_SM;
    } else if (windowWidthVp >= 600 && windowWidthVp < 840) {
      widthBp = BreakpointConstants.BREAKPOINT_MD;
    } else if (windowWidthVp >= 840 && windowWidthVp < 1440) {
      widthBp = BreakpointConstants.BREAKPOINT_LG;
    } else {
      widthBp = BreakpointConstants.BREAKPOINT_XL;
    }
    Logger.info('Window width: ' + windowWidth + ', width breakpoint: ' + widthBp);
    AppStorage.setOrCreate('currentBreakpoint', widthBp);
  }

  private updateHeightBp(windowWidth: number, windowHeight: number): void {
    // if (this.windowObj === undefined) {
    //   return;
    // }
    // let mainWindow: window.WindowProperties = this.windowObj.getWindowProperties();
    // let windowHeight: number = mainWindow.windowRect.height;
    // let windowWidth: number = mainWindow.windowRect.width;
    let windowWidthVp = windowWidth / display.getDefaultDisplaySync().densityPixels;
    let windowHeightVp = windowHeight / display.getDefaultDisplaySync().densityPixels;
    let heightBp: string = '';
    let aspectRatio: number = windowHeightVp / windowWidthVp;
    if (aspectRatio < 0.8) {
      heightBp = BreakpointConstants.BREAKPOINT_SM;
    } else if (aspectRatio >= 0.8 && aspectRatio < 1.2) {
      heightBp = BreakpointConstants.BREAKPOINT_MD;
    } else {
      heightBp = BreakpointConstants.BREAKPOINT_LG;
    }
    Logger.info('Aspect ratio: ' + aspectRatio + ', height breakpoint: ' + heightBp);
    AppStorage.setOrCreate('currentHeightBreakpoint', heightBp);
  }
}

