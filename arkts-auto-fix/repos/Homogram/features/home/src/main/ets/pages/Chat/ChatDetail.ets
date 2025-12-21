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

import { Adaptive, BaseConstants, BreakpointConstants, Logger } from '@ohos/base';
import { ChatDetailBottom } from '../../views/Chat/ChatDetailBottom';
import ChatDetailItem from '../../views/Chat/ChatDetailItem';
import { ChatDetailTopSearch } from '../../views/Chat/ChatDetailTopSearch';
import { HomeConstants } from '../../constants/HomeConstants';
import { deviceInfo } from '@kit.BasicServicesKit';
import { Message } from '../../entities/Message';
import { Chat } from '../../entities/Chat';
import { HashMap } from '@kit.ArkTS';
import { ChatDataSource } from '../../viewmodel/Chat/ChatDataSource';
import { MessageDataSource } from '../../viewmodel/Message/MessageDataSource';
import { ChatType } from 'libhomogrape.so';
import { curves } from '@kit.ArkUI';

@AnimatableExtend(NavDestination)
function animatableBlur(blurRadius: number) {
  .blur(blurRadius)
}

@Component
export struct ChatDetail {
  @StorageProp('currentBreakpoint') currentBreakpoint: string = BreakpointConstants.BREAKPOINT_SM;
  // @Consume pinnedChatsMap: HashMap<string, Chat>;
  // @Consume chatsMap: Map<string, Chat>;
  @Consume chatDataSource: ChatDataSource;
  @Consume messagesOfChatMap: HashMap<string, MessageDataSource>;
  @Consume currentChatId: string;
  @Consume('pageInfo') pageInfo: NavPathStack;
  @State currentChatUserName: string = '';
  @State isShowMediaSheet: boolean = false;
  @State blurStyle: BlurStyle = BlurStyle.Thin;
  @State titleAndTabBarColor: Color | Resource = Color.Transparent;
  @Prop isInChatDetailPage: boolean;
  @Provide continuousChatDetailBlurRadius: number = 0;
  @Provide discreteChatDetailBlurRadius: number = 0;
  @Provide isScrollingDueToReplyToMessage: boolean = false;
  @Provide scrollToRepliedMessageIndex: number = -1;
  @State messageTransitionEffect: TransitionEffect | undefined = undefined;
  private shouldTransferToOnScrollIndex = false;
  private beforeFinalScrollIndex: number = -1;
  private messageListScroller: ListScroller = new ListScroller();
  private data: MessageDataSource = new MessageDataSource();
  private isAtEnd: boolean = false;
  private length: number = 0;
  private chatType: ChatType = ChatType.User;

  @Styles
  messageAnimationStyle() {
    .animation({ curve: curves.springMotion(0.6, 0.8) })
    .transition(this.messageTransitionEffect)
  }

  // @Builder
  // NavigationTitle() {
  //   Row() {
  //     Text(this.currentChatUserName)
  //       .fontColor($r('sys.color.font_primary'))
  //       .fontSize(HomeConstants.NAV_TITLE_FONT_SIZE_LARGE)
  //       .lineHeight(41)
  //       .fontWeight(700)
  //       .gesture(GestureGroup(GestureMode.Exclusive,
  //         TapGesture({ count: 2 })
  //           .onAction(() => {
  //             console.info('TapGesture 2');
  //             let scroller = AppStorage.get(HomeConstants.CHAT_LIST_SCROLLER) as Scroller;
  //             Logger.debug('Home page title onAction TapGesture 2 scroller is ' + scroller);
  //             scroller.scrollToIndex(0, true);
  //           }),
  //         TapGesture({ count: 1 })
  //           .onAction(() => {
  //             console.info('TapGesture 1'); // ignore
  //           })
  //       ))
  //   }
  // }

  build() {
    NavDestination() {
      Stack({ alignContent: Alignment.Bottom }) {

        // Row() {
        //   ChatDetailTopSearch({
        //     currentChatUserName: this.currentChatUserName,
        //   })
        // }
        // .height(Adaptive.ContactItemHeight(this.currentBreakpoint))
        // .expandSafeArea([SafeAreaType.KEYBOARD])
        // .zIndex(1)
        // .backgroundColor($r('app.color.background_color_grey'))

        List({
          initialIndex: this.length - 1,
          scroller: this.messageListScroller
        }) {
          LazyForEach(this.messagesOfChatMap.get(this.currentChatId), (item: Message, index: number) => {
            ListItem() {
              ChatDetailItem({
                chatType: this.chatType,
                isAppletMsg: false,
                isDocumentMsg: false,
                message: item,
                messageListScroller: this.messageListScroller,
              })
            }
            .margin({
              top: index === 0 ? HomeConstants.NAV_TITLE_TOP_MARGIN_MINI : 0,
              bottom: index === this.length - 1 ? HomeConstants.NAV_SIGNAL_BAR_HEIGHT : 0
            })
          }, (item: Message, index: number) => index + JSON.stringify(item))
        }
        .height(BaseConstants.FULL_HEIGHT)
        .width(BaseConstants.FULL_WIDTH)
        .edgeEffect(EdgeEffect.Spring, { alwaysEnabled: true })
        .messageAnimationStyle()
        .onAppear(() => {
          this.messageTransitionEffect = TransitionEffect.move(TransitionEdge.BOTTOM)
            .animation({ curve: curves.springMotion(0.6, 0.8) });
        })
        .cachedCount(BaseConstants.LAZY_FOR_EACH_CACHED_COUNT)
        .onReachEnd(() => {
          this.isAtEnd = true;
        })
        .onDidScroll((scrollOffset: number, scrollState: ScrollState) => {
          // Logger.debug('ChatDetail onDidScroll scrollOffset: ' + scrollOffset + ' scrollState: ' + scrollState);
          if (this.isScrollingDueToReplyToMessage) {
            if (scrollState === ScrollState.Fling && Math.abs(scrollOffset) < 20) {
              Logger.debug('ChatDetail onDidScroll isScrollingDueToReplyToMessage: ' +
              this.scrollToRepliedMessageIndex);
              this.isScrollingDueToReplyToMessage = false;

              Logger.debug('ChatDetail onDidScroll isScrollingDueToReplyToMessage smoothly scrollToIndex sub-step 1: ' +
                (this.scrollToRepliedMessageIndex + BaseConstants.LAZY_FOR_EACH_SCROLL_SMOOTH_BUFFER));
              this.messageListScroller.scrollToIndex(this.scrollToRepliedMessageIndex +
              BaseConstants.LAZY_FOR_EACH_SCROLL_SMOOTH_BUFFER, false, ScrollAlign.CENTER);

              this.shouldTransferToOnScrollIndex = true;
              this.beforeFinalScrollIndex = this.scrollToRepliedMessageIndex +
              BaseConstants.LAZY_FOR_EACH_SCROLL_SMOOTH_BUFFER;
              Logger.debug('ChatDetail onDidScroll isScrollingDueToReplyToMessage smoothly scrollToIndex sub-step 1 beforeFinalScrollIndex: ' +
              this.beforeFinalScrollIndex);
              this.scrollToRepliedMessageIndex = -1;
            }
          }
        })
        .onScrollIndex((start, end, center) => {
          Logger.debug('ChatDetail onScrollIndex start: ' + start + ' end: ' + end + ' center: ' + center);
          if (this.shouldTransferToOnScrollIndex) {
            if (center === this.beforeFinalScrollIndex) {
              this.shouldTransferToOnScrollIndex = false;

              Logger.debug('ChatDetail onScrollIndex isScrollingDueToReplyToMessage smoothly scrollToIndex sub-step 2: ' +
              this.beforeFinalScrollIndex);
              this.messageListScroller.scrollToIndex(this.beforeFinalScrollIndex -
              BaseConstants.LAZY_FOR_EACH_SCROLL_SMOOTH_BUFFER, true, ScrollAlign.CENTER);

              this.beforeFinalScrollIndex = -1;
            }
          }
        })

        Row() {
          ChatDetailBottom({
            isShowMediaSheet: this.isShowMediaSheet,
          })
        }
      }
      .height(BaseConstants.FULL_HEIGHT)
      .width(BaseConstants.FULL_WIDTH)
    }
    .expandSafeArea([SafeAreaType.SYSTEM])
    .title(this.currentChatUserName, {
      backgroundColor: this.titleAndTabBarColor,
      backgroundBlurStyle: this.blurStyle,
      barStyle: BarStyle.STACK,
    })
    // .hideTitleBar(true)
    .blur(this.isShowMediaSheet ? this.continuousChatDetailBlurRadius : 0)

    // .animatableBlur(this.discreteChatDetailBlurRadius == 0 ? 0 : this.continuousChatDetailBlurRadius)
  }

  aboutToAppear(): void {
    // this.navTitleMode = NavigationTitleMode.Mini;
    let chat = this.chatDataSource.get(this.currentChatId);
    this.chatType = chat?.chatType || ChatType.User;
    this.currentChatUserName = chat?.name || '';
    this.length = this.messagesOfChatMap.get(this.currentChatId)?.totalCount() || 0;
    // this.messages = (this.currentChatPinned ? this.pinnedChatsMap :
    // this.chatsMap).get(this.currentChatId)?.sortedMessages || [];
  }

  aboutToDisappear(): void {
  }
}