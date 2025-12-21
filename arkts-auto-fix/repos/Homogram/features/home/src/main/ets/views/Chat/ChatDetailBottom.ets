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
import { StandardIcon } from '@ohos/base/src/main/ets/views/image/StandardIcon';
import { HomeConstants } from '../../constants/HomeConstants';
import { NativeMessage, sendMessage } from 'libhomogrape.so'
import { Logger } from '@ohos/base';
import { Chat } from '../../entities/Chat';
import { Message } from '../../entities/Message';
import { ChatDetailBottomPhotoPicker } from './ChatDetailBottomPhotoPicker';
import { ChatDetailBottomMediaSheet } from './ChatDetailBottomMediaSheet';
import { curves } from '@kit.ArkUI';
import { HashMap } from '@kit.ArkTS';
import { ChatDataSource } from '../../viewmodel/Chat/ChatDataSource';
import { MessageDataSource } from '../../viewmodel/Message/MessageDataSource';

const MAX_BLUR_RADIUS = 20;

@AnimatableExtend(Tabs)
function animatableBarHeight(height: number | string) {
  .barHeight(height)
}

@Component
export struct ChatDetailBottom {
  @State text: string = "";
  @Consume('pageInfo') pageInfo: NavPathStack;
  // @Consume pinnedChatsMap: HashMap<string, Chat>;
  // @Consume chatsMap: Map<string, Chat>;
  @Consume chatDataSource: ChatDataSource;
  @Consume messagesOfChatMap: HashMap<string, MessageDataSource>;
  @Consume discreteChatDetailBlurRadius: number;
  @Consume continuousChatDetailBlurRadius: number;
  @State animate: boolean = false;
  @State shouldShowGrid: boolean = false;
  @Link isShowMediaSheet: boolean;
  @State isShowMediaBarSheet: boolean = false;
  @State selectedMediaCount: number = 0;
  @State currentDetents: number = 0; // 0 means medium, 1 means full
  @State mediaSheetTitle: Resource = $r('app.string.gallery');
  @State selectedMedias: Array<string> = [];
  @Consume currentChatId: string;

  @Builder
  MediaTab() {

  }

  @Builder
  MediaSheet() {
    // Flex() {
    //   Tabs({ barPosition: BarPosition.End }) {
    //     TabContent() {
    //       Flex({ direction: FlexDirection.Column }) {
    //         ChatDetailBottomPhotoPicker({
    //           selectedMediaCount: $selectedMediaCount,
    //           animate: $animate,
    //         })
    //       }
    //     }
    //     .tabBar("Photo")
    //
    //     TabContent() {
    //       // Text("Files").fontSize(30)
    //       // ChatDetailBottomMediaSheet()
    //     }
    //     .tabBar("Files")
    //   }
    //   .barOverlap(true)
    //   .barBackgroundBlurStyle(BlurStyle.BACKGROUND_THIN)
    //   .barHeight(0)
    //   // .animatableBarHeight(this.selectedMediaCount != 0 ? 0 : 80)
    //   .onAnimationStart(() => {
    //     Logger.debug("MediaSheet onAnimationStart");
    //   })
    //   .onAnimationEnd(() => {
    //     Logger.debug("MediaSheet onAnimationEnd");
    //   })
    //   .animation({ curve: curves.springMotion() })
    // }
    // .height("100%")
    // .width("100%")

    Flex({ direction: FlexDirection.Column }) {
      Scroll() {
        ChatDetailBottomMediaSheet({
          selectedMediaCount: $selectedMediaCount,
          animate: $animate,
          text: $text,
          sendMessage: this.sendMessage,
          currentDetents: this.currentDetents,
          title: $mediaSheetTitle,
          selectedMedias: $selectedMedias,
        })
      }
      .nestedScroll({
        scrollForward: NestedScrollMode.PARENT_FIRST,
        scrollBackward: NestedScrollMode.SELF_ONLY,
      })
    }
    .height("100%")
    .width("100%")
  }

  build() {
    Flex({ alignItems: ItemAlign.Center }) {
      Column() {
        StandardIcon({ icon: $r('app.media.ic_public_app') })
      }
      .flexBasis(HomeConstants.FLEX_BASIS_AUTO)
      .padding({
        right: $r('app.float.chat_detail_bottom_padding'),
        left: $r('app.float.chat_detail_bottom_padding'),
      })
      .bindSheet($$this.isShowMediaSheet, this.MediaSheet(), {
        height: "75%",
        dragBar: false,
        detents: [SheetSize.MEDIUM, "100%"],
        scrollSizeMode: ScrollSizeMode.CONTINUOUS,
        onAppear: () => {
        },
        onHeightDidChange: (height: number) => {
          Logger.debug("MediaSheet onHeightDidChange: " + height);
          this.continuousChatDetailBlurRadius = height / (700 * 3.25) * MAX_BLUR_RADIUS;
        },
        onWillDisappear: () => {
          Logger.debug("MediaSheet onWillDisappear");
          this.discreteChatDetailBlurRadius = 0;
          this.isShowMediaSheet = false;
        },
        onDisappear: () => {
          Logger.debug("MediaSheet onDisappear before unset isShowMediaSheet state: " + this.isShowMediaSheet);
          this.isShowMediaSheet = false;
          this.selectedMediaCount = 0;
          Logger.debug("MediaSheet onDisappear after unset isShowMediaSheet state: " + this.isShowMediaSheet);
        },
        onDetentsDidChange: (detents: number) => {
          Logger.debug("MediaSheet onDetentsDidChange: " + detents);
          this.currentDetents = detents;
        },
        title: {
          title: this.mediaSheetTitle
        }
      })
      .onClick(() => {
        Logger.debug("MediaSheet onClick");
        this.isShowMediaSheet = !this.isShowMediaSheet;
        Logger.debug("MediaSheet onclick current isShowMediaSheet state: " + this.isShowMediaSheet);
      })

      Blank()
      Column() {
        TextArea({
          text: this.text,
        })// .placeholderColor($r('app.color.text_input_default_color'))
          // .caretColor($r('app.color.text_input_default_care_color'))
          // .backgroundColor($r('app.color.background_color_grey'))
          .opacity(0.5)
          .borderRadius(HomeConstants.CHAT_DETAIL_BOTTOM_TEXT_RADIUS)
          .flexGrow(BaseConstants.FLEX_GROW_ONE)
          .padding({ right: $r('app.float.chat_detail_bottom_text_padding') })
          .enterKeyType(EnterKeyType.Send)
          .onChange((text: string) => {
            this.text = text;
          })
          .onSubmit((enterKey: EnterKeyType) => {
            if (enterKey == EnterKeyType.Send) {
              try {
                this.sendMessage();
                Logger.debug("sendMessage: " + this.text);
              } catch (e) {
                Logger.error("sendMessage error: " + e.message);
              }
            }
          })
      }
      .flexBasis(HomeConstants.FLEX_BASIS_AUTO)


      Column() {
        StandardIcon({ icon: $r('app.media.ic_public_emoji') })
      }
      .flexBasis(HomeConstants.FLEX_BASIS_AUTO)
      .padding({
        left: $r('app.float.chat_detail_bottom_padding'),
        right: $r('app.float.chat_detail_bottom_padding'),
      })


      Column() {
        StandardIcon({ icon: $r('app.media.ic_public_add_norm') })
      }
      .padding({
        right: $r('app.float.chat_detail_bottom_padding'),
        left: $r('app.float.chat_detail_bottom_padding'),
      })
    }
    .padding({
      top: $r('app.float.chat_detail_bottom_padding'),
      bottom: $r('app.float.chat_detail_bottom_padding'),
    })
    .width(BaseConstants.FULL_WIDTH)
    // .height($r('app.float.chat_detail_bottom_height'))
    // .backgroundColor($r('app.color.background_color_grey'))
    .backgroundBlurStyle(BlurStyle.Thin)
    .backgroundColor(Color.Transparent)
    .expandSafeArea([SafeAreaType.SYSTEM])
  }

  sendMessage() {
    if (this.currentChatId) {
      // send message
      Logger.debug("sendMessage: " + this.text + " to " + this.currentChatId);
      sendMessage(parseInt(this.currentChatId), this.text, undefined, this.updateProgress)
        .then((message: NativeMessage[]) => {
          Logger.debug("sendMessage success: " + message[0].text);
          let chat = this.chatDataSource.get(this.currentChatId);
          if (chat) {
            this.messagesOfChatMap.get(this.currentChatId)?.push(Message.fromNative(message[0]));
            chat.lastMessageId = message[0].messageId;
            chat.lastMessageSenderName = message[0].senderName;
            chat.lastMessageText = message[0].text;
            chat.lastMessageTimestamp = message[0].timestamp;
            this.chatDataSource.add(chat);
          }
        })
        .catch((err: Error) => {
          Logger.error("sendMessage error: " + err.message);
        });
      this.text = "";
    }
  }

  async updateProgress(media_index: number, progress: number) {
    // TODO: implement update media sending progress
    Logger.debug("updateProgress: " + media_index + " " + progress);
  }
}