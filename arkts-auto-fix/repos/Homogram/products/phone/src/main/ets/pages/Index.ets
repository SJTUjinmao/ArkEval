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

import { Home } from '@ohos/home';
import { BaseConstants, BreakpointConstants } from '@ohos/base';

@Entry
@Component
export default struct Index {
  @StorageLink('currentBreakpoint') currentBreakpoint: string = BreakpointConstants.BREAKPOINT_SM;

  build() {
    GridRow({
      columns: {
        sm: BreakpointConstants.GRID_ROW_COLUMNS[2],
        md: BreakpointConstants.GRID_ROW_COLUMNS[0],
        lg: BreakpointConstants.GRID_ROW_COLUMNS[0]
      }
    }) {
      GridCol({
        span: {
          sm: BreakpointConstants.GRID_COLUMN_SPANS[5],
          md: BreakpointConstants.GRID_COLUMN_SPANS[0],
          lg: BreakpointConstants.GRID_COLUMN_SPANS[0]
        }
      }) {
        Column() {
          Home()
        }
        .width(BaseConstants.FULL_HEIGHT)
        .height(BaseConstants.FULL_HEIGHT)
      }
      .height(BaseConstants.FULL_PERCENT)
      .width(BaseConstants.FULL_PERCENT)
    }
    .onBreakpointChange((breakPoints) => {
      this.currentBreakpoint = breakPoints;
    })
  }
}