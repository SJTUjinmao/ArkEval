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

/**
 * Constants for breakpoint.
 */
export class BreakpointConstants {
  /**
   * Breakpoints that represent smaller device types.
   */
  static readonly BREAKPOINT_XS: string = 'xs';
  /**
   * Breakpoints that represent small device types.
   */
  static readonly BREAKPOINT_SM: string = 'sm';
  /**
   * Breakpoints that represent middle device types.
   */
  static readonly BREAKPOINT_MD: string = 'md';
  /**
   * Breakpoints that represent large device types.
   */
  static readonly BREAKPOINT_LG: string = 'lg';
  /**
   * Breakpoints that represent larger device types.
   */
  static readonly BREAKPOINT_XL: string = 'xl';
  static readonly BREAKPOINT_SCOPE: number[] = [0, 320, 600, 840];
  /**
   * Grid row column list.
   */
  static readonly GRID_ROW_COLUMNS: number[] = [12, 15, 4];
  /**
   * Grid column span list.
   */
  static readonly GRID_COLUMN_SPANS: number[] = [12, 6, 7, 5, 3, 4, 2];
}