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

import { BaseConstants, BreakpointConstants, Logger } from '@ohos/base';
import { HomeConstants } from '../../constants/HomeConstants';
import { DateHelper } from '@ohos/base';
import { Chat } from '../../entities/Chat';
import { image } from '@kit.ImageKit';
import { ListFileOptions } from '@kit.CoreFileKit';
import { fileIo as fs, ReadOptions } from '@kit.CoreFileKit';
import { getProfilePhotoPathAndCount, downloadProfilePhoto, ChatType } from 'libhomogrape.so'
import { HashMap } from '@kit.ArkTS';
import { SeenChat } from '../../entities/SeenChat';
import { getOrDownloadProfilePhoto, getThumbProfilePhotoPixelMap } from '../../utils';
import {
  ChatPhotoNodeController,
  createChatPhotoNode,
  getChatPhotoNode
} from '../../viewmodel/Chat/ChatPhotoNodeController';

@Component
export struct ChatItem {
  @StorageProp('currentBreakpoint') currentBreakpoint: string = BreakpointConstants.BREAKPOINT_SM;
  @Consume seenChatsMap: HashMap<string, SeenChat>;
  @State isPixelMapReady: boolean = false;
  @State profilePhotoPixelMap: PixelMap | undefined = undefined;
  @State profilePhotoPath: string | undefined = undefined;
  @State profilePhotoLoaded: boolean = false;
  @State profilePhotoLoadFailed: boolean = false;
  @State chatPhotoNodeController: ChatPhotoNodeController | undefined = undefined;
  private chatData?: Chat;
  private isInTopicPage: boolean = false;

  build() {
    Flex({ alignItems: ItemAlign.Center }) {

      NodeContainer(this.chatPhotoNodeController)
        .margin({ left: $r('app.float.icon_margin_one'), right: $r('app.float.icon_margin_one') })

      if (!this.isInTopicPage) {
        Flex({ direction: FlexDirection.Column, justifyContent: FlexAlign.Start, alignItems: ItemAlign.Start }) {
          // Column() {
          Text(this.chatData?.name)
            .maxLines(HomeConstants.CHAT_LIST_NAME_MAX_LINE)
            .fontSize(BaseConstants.FONT_SIZE_SIXTEEN)
            .fontWeight(BaseConstants.FONT_WEIGHT_FIVE)
            .lineHeight($r('app.float.text_line_height_two'))
            .fontFamily(BaseConstants.FONT_FAMILY_MEDIUM)
          if (this.chatData?.chatType === ChatType.Group) {
            Text(this.chatData?.lastMessageSenderName)
              .maxLines(HomeConstants.CHAT_LIST_NAME_MAX_LINE)
              .fontSize(BaseConstants.FONT_SIZE_FIFTEEN)
              .fontWeight(BaseConstants.FONT_WEIGHT_FOUR)
              .lineHeight($r('app.float.text_line_height_one'))
              .fontFamily(BaseConstants.FONT_FAMILY_NORMAL)
          }
          Text(this.chatData?.lastMessageText)
            .maxLines(HomeConstants.CHAT_DETAIL_MAX_LINE)
            .textOverflow({ overflow: TextOverflow.Ellipsis })
            .fontSize(BaseConstants.FONT_SIZE_FOURTEEN)
            .fontWeight(BaseConstants.FONT_WEIGHT_FOUR)
            .lineHeight($r('app.float.text_line_height_one'))
            .fontFamily(BaseConstants.FONT_FAMILY_NORMAL)
            .fontColor(Color.Grey)
            .width(BaseConstants.FULL_WIDTH)
        }
        // .flexShrink(BaseConstants.FLEX_SHRINK_ONE)
        // .flexGrow(BaseConstants.FLEX_GROW_ONE)
        .margin({ left: $r('app.float.icon_margin_one') })

        Blank()
        Column() {
          Text(DateHelper.getParsedDateString(this.chatData?.lastMessageTimestamp))
            .fontWeight(BaseConstants.FONT_WEIGHT_FOUR)
            .fontSize(BaseConstants.FONT_SIZE_TEN)
            .fontColor(Color.Grey)
            .fontFamily(BaseConstants.FONT_FAMILY_NORMAL)
            .width($r('app.float.chat_list_time_width'))
        }
        .flexBasis(HomeConstants.FLEX_BASIS_AUTO)
        .margin({ left: $r('app.float.icon_margin_two'), right: $r('app.float.icon_margin_one') })

        // .padding($r('app.float.chat_list_time_padding'))
      }
    }
    .width(BaseConstants.FULL_WIDTH)
    .padding({
      top: HomeConstants.CHAT_LIST_ITEM_MARGIN,
      bottom: HomeConstants.CHAT_LIST_ITEM_MARGIN,
    })
  }

  async aboutToAppear(): Promise<void> {
    let node = getChatPhotoNode(this.chatData?.chatId);
    if (node === undefined) {
      this.profilePhotoPath = await getOrDownloadProfilePhoto(this.chatData?.chatId || 0);
      this.profilePhotoLoaded = true;
      let thumb = await getThumbProfilePhotoPixelMap(this.seenChatsMap.get(this.chatData?.chatId.toString())?.photoThumb);
      this.profilePhotoPixelMap = thumb;
      this.isPixelMapReady = true;
      createChatPhotoNode(this.getUIContext(), this.chatData?.chatId, this.profilePhotoPath, this.profilePhotoPixelMap);
      this.chatPhotoNodeController = getChatPhotoNode(this.chatData?.chatId);
    } else {
      this.chatPhotoNodeController = node;
    }
  }
}