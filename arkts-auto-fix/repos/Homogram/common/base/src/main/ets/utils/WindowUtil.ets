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

import { deviceInfo } from '@kit.BasicServicesKit';
import { window } from '@kit.ArkUI';
import { BaseConstants } from '../constants/BaseConstants';
import Logger from './Logger/Logger';

@Observed
export class WindowUtil {
  public static window: window.Window;
  public static windowWidth_px: number;
  public static windowHeight_px: number;
  public static topAvoidAreaHeight_px: number;
  public static navigationIndicatorHeight_px: number;
  private windowStage?: window.WindowStage;
  private mainWindowClass?: window.Window;

  static getInstance(): WindowUtil | undefined {
    if (!AppStorage.get<WindowUtil>(BaseConstants.WINDOW_UTIL)) {
      AppStorage.setOrCreate(BaseConstants.WINDOW_UTIL, new WindowUtil());
    } else {
      Logger.info(`AppStorage does not have windowUtil`);
    }
    return AppStorage.get<WindowUtil>(BaseConstants.WINDOW_UTIL);
  }

  setWindowStage(windowStage: window.WindowStage): void {
    this.windowStage = windowStage;
  }

  setMainWindowPortrait(): void {
    if (this.windowStage === undefined) {
      Logger.error(`WindowStage is undefined`);
      return;
    }
    // Obtain the main window of the application.
    this.windowStage.getMainWindow((err, windowClass: window.Window) => {
      this.mainWindowClass = windowClass;
      if (err.code) {
        Logger.error(`Failed to obtain the main window. Code:${err.code}, message:${err.message}`);
        return;
      }
      if (deviceInfo.deviceType !== BaseConstants.DEVICE_TYPES[0] && deviceInfo.deviceType !==
      BaseConstants.DEVICE_TYPES[1]) {
        // Set portrait display.
        this.mainWindowClass.setPreferredOrientation(window.Orientation.PORTRAIT);
      }
    });
  }

  setMainWindowOrientation(orientation: window.Orientation): void {
    if (this.mainWindowClass === undefined) {
      Logger.error(`MainWindowClass is undefined`);
      return;
    }
    // Setting orientation.
    this.mainWindowClass.setPreferredOrientation(orientation);
  }

  disableWindowSystemBar(): void {
    if (this.mainWindowClass === undefined) {
      Logger.error(`MainWindowClass is undefined`);
      return;
    }
    // Set the status bar and navigation bar to be invisible in full-screen mode.
    this.mainWindowClass.setWindowSystemBarEnable([]);
  }

  enableWindowSystemBar(): void {
    if (this.mainWindowClass === undefined) {
      Logger.error(`MainWindowClass is undefined`);
      return;
    }
    this.mainWindowClass.setWindowSystemBarEnable(['status', 'navigation']);
  }

  setFullScreen(): void {
    if (this.mainWindowClass === undefined) {
      Logger.error(`MainWindowClass is undefined`);
      return;
    }
    // Set full-screen display.
    this.mainWindowClass.setWindowLayoutFullScreen(true, (err) => {
      if (err.code) {
        Logger.error('Failed to set the window layout to full-screen mode. Cause:' + JSON.stringify(err));
        return;
      }
      Logger.info('Succeeded in setting the window layout to full-screen mode.');
    });
  }

  getMainWindow(): window.Window | undefined {
    return this.mainWindowClass;
  }

  offWindowSizeChange(): void {
    if (this.mainWindowClass === undefined) {
      Logger.error(`MainWindowClass is undefined`);
      return;
    }
    this.mainWindowClass?.off('windowSizeChange');
  }
}