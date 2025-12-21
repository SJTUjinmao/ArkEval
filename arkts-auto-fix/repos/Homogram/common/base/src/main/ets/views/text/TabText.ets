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

import { BaseConstants } from '../../constants/BaseConstants';

@Component
export struct TabText {
  @Prop data: string = '';
  @Prop resourceData: Resource;
  @Prop fontSize: Resource = BaseConstants.SMALL_MINI_FONT_SIZE;
  @Prop fontColor: Resource = $r('app.color.font_color_one');
  @Prop fontWeight: number = BaseConstants.FONT_WEIGHT_FIVE;
  @Prop fontFamily: Resource = BaseConstants.FONT_FAMILY_MEDIUM;

  build() {
    Text(this.data || this.resourceData)
      .fontFamily(this.fontFamily)
      .fontSize(this.fontSize)
      .fontColor(this.fontColor)
      .textAlign(TextAlign.Center)
      .lineHeight($r('app.float.text_line_height_five'))
      .fontWeight(this.fontWeight)
  }
}