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

import {
  Adaptive,
  BaseConstants,
  BreakpointConstants,
  CurrentFeature,
  CurrentPage,
  Logger,
  delay,
  stringToBuffer,
  mapToRecord,
  StandardIcon,
  CircleButton,
  Vibrator
} from '@ohos/base';
import ChatDetailNone from '../views/Chat/ChatDetailNone';
import {
  IncomingMessageCallback,
  UpdateChatCallback,
  loadCachedChats,
  cacheOrUpdateChatPair,
  cacheOrUpdateMessage,
  cacheOrUpdateMessages,
  cacheOrUpdateChat
} from '../viewmodel/Chat/ChatViewModel'
import { ChatDetail } from './Chat/ChatDetail';
import { ChatList } from '../views/Chat/ChatList';
import { HomeConstants } from '../constants/HomeConstants';
import { deviceInfo, emitter } from '@kit.BasicServicesKit';
import { PhoneNumber, VerifyCode, Password } from './Login/Index'
import {
  isLoggedIn,
  run,
  registerCacheSeenChatCallback,
  registerUpdateChatCallback,
  registerIncomingMessageCallback,
  loadChatsWithOffset,
  NativePackedChat,
  NativeRawMessage,
  NativeChat,
  NativeMessage,
  NativeSeenChat,
  getMe,
  reconnect,
} from 'libhomogrape.so'
import { Chat } from '../entities/Chat';
import { Message } from '../entities/Message';
import { HashMap } from '@kit.ArkTS';
import { ChatDataSource } from '../viewmodel/Chat/ChatDataSource';
import { MessageDataSource } from '../viewmodel/Message/MessageDataSource';
import { curves, SymbolGlyphModifier } from '@kit.ArkUI';
import { SeenChat } from '../entities/SeenChat';
import { seenChatDao } from '../dao/SeenChatDao';
import { ForumTopicList } from './Chat/Group/ForumTopicList';
import { EventConstants } from '../constants/EventConstants';


@Entry
@Component
export struct Index {
  @StorageProp('currentBreakpoint') @Watch('watchCurrentBreakpoint') currentBreakpoint: string =
    BreakpointConstants.BREAKPOINT_SM;
  @StorageProp('bottomRectHeight') bottomRectHeight: number = 0;
  @StorageProp('topRectHeight') topRectHeight: number = 0;
  @Provide chatDataSource: ChatDataSource = new ChatDataSource();
  @Provide messagesOfChatMap: HashMap<string, MessageDataSource> = new HashMap();
  @Provide seenChatsMap: HashMap<string, SeenChat> = new HashMap();
  @Provide('pageInfo') pageInfo: NavPathStack = new NavPathStack();
  @Provide me: NativeSeenChat | undefined = undefined;
  @Provide currentChatId: string = '';
  @Provide currentForumId: string = '';
  @State currentChatPinned: boolean = false;
  @State currentChatUserName: string = '';
  @State currentContactUserName: string = '';
  @State currentContactUserIcon: Resource = HomeConstants.DEFAULT_AVATAR;
  @State currentPageIndex: number = CurrentPage.HOME;
  @State currentFeatureIndex: Number = CurrentFeature.HOME;
  @State isSplitAppletScreen: boolean = BaseConstants.SPLIT_APPLET_SCREEN;
  @State isSplitDocumentScreen: boolean = BaseConstants.SPLIT_DOCUMENT_SCREEN;
  @State isLoggedIn: boolean = false;
  @State blurStyle: BlurStyle = BlurStyle.Thin;
  @State titleAndTabBarColor: Color | Resource = Color.Transparent;
  @State isShowNewMessageSheet: boolean = false;
  @State isShowSearchIcon: boolean = false;
  @State isLoadingNewMessages: boolean = false;
  @State isShowChatListTopPadding: boolean = false;
  @State titleMode: NavigationTitleMode = NavigationTitleMode.Free;
  @State barStyle: BarStyle = BarStyle.STANDARD;
  @State isInChatDetailPage: boolean = false;
  @State isOnTop: boolean = true;
  @Provide isAppFirstLoad: boolean = true;
  @State menuItems: Array<NavigationMenuItem> = [
    {
      value: 'menuItem1',
      symbolIcon: new SymbolGlyphModifier($r('sys.symbol.ohos_folder_badge_plus')).fontColor([Color.Red, Color.Green])
        .renderingStrategy(SymbolRenderingStrategy.MULTIPLE_COLOR),
    },
  ];
  private searchIconTransitionEffect: TransitionEffect =
    // 创建默认透明度转场效果,并指定了springMotion(0.6, 0.8)曲线
    TransitionEffect.OPACITY.animation({
      curve: curves.springMotion(0.6, 0.8)
    })// 通过combine方法,这里的动画参数会跟随上面的TransitionEffect，也就是springMotion(0.6, 0.8)
      .combine(TransitionEffect.scale({
        x: 0,
        y: 0
      }))// 添加旋转转场效果，这里的动画参数会跟随上面带animation的TransitionEffect，也就是springMotion(0.6, 0.8)
      .combine(TransitionEffect.rotate({ angle: 90 }))// 添加平移转场效果，这里的动画参数使用指定的springMotion()
      .combine(TransitionEffect.translate({ y: 150 })
        .animation({ curve: curves.springMotion() }))// 添加move转场效果，这里的动画参数会跟随上面的TransitionEffect，也就是springMotion()
      .combine(TransitionEffect.move(TransitionEdge.END)); // 添加move转场效果，这里的动画参数会跟随上面的TransitionEffect，也就是springMotion()
  @State menuItems2: Array<NavigationMenuItem> = [
    {
      value: 'menuItem1',
      symbolIcon: new SymbolGlyphModifier($r('sys.symbol.ohos_folder_badge_plus')).fontColor([Color.Red, Color.Green])
        .renderingStrategy(SymbolRenderingStrategy.MULTIPLE_COLOR)
    },
    {
      value: 'menuItem2',
      symbolIcon: new SymbolGlyphModifier($r('sys.symbol.magnifyingglass_circle')).transition(
        this.searchIconTransitionEffect
      )
    },
  ]
  private allowedCustomTransitionFromPageName: string[] = ['undefined'];
  private allowedCustomTransitionToPageName: string[] = ['TopicList'];
  private updateChatCallback: UpdateChatCallback =
    async (err: Error | null, seenChat: NativeSeenChat, chat: NativeChat,
      messages: NativeMessage[]) => {
      if (err) {
        Logger.error(`updateChatCallback failed, message is ${err.message}`);
        return;
      }
      if (chat) {
        Logger.debug(`updateChatCallback before chatDataSource.add chat.name is ${chat.name}`);
        this.chatDataSource.add(Chat.fromNative(chat));
        // TODO: we need to load the messages upon the appearance of the ChatDetail page
        let messageDataSource = this.messagesOfChatMap.get(chat.chatId.toString()) || new MessageDataSource();
        let new_messages = await Promise.all(messages.map(async (message) => {
          return Message.fromNative(message);
        }));
        messageDataSource.append(new_messages);
        this.messagesOfChatMap.set(chat.chatId.toString(), messageDataSource);

        this.cacheThings(seenChat, chat, new_messages);
      }
    }

  watchCurrentBreakpoint() {
    if (this.currentBreakpoint === BreakpointConstants.BREAKPOINT_SM &&
      this.pageInfo.getParamByName('ChatDetailNone').length === 1 && this.pageInfo.size() === 1) {
      this.pageInfo.pop();
    }
  }

  @Builder
  NavigationTitle() {
    Row() {
      if (this.isLoadingNewMessages && this.currentPageIndex === 0) {
        LoadingProgress()
          .size({
            width: HomeConstants.NAV_TITLE_FONT_SIZE_LARGE,
            height: HomeConstants.NAV_TITLE_FONT_SIZE_LARGE
          })
        Text('加载中...')
          .fontColor('#182431')
          .fontSize(HomeConstants.NAV_TITLE_FONT_SIZE_LARGE)
          .lineHeight(41)
          .fontWeight(700)
          .gesture(GestureGroup(GestureMode.Exclusive,
            TapGesture({ count: 2 })
              .onAction(() => {
                console.info('TapGesture 2');
                if (this.currentPageIndex === 0) {
                  let scroller = AppStorage.get(HomeConstants.CHAT_LIST_SCROLLER) as Scroller;
                  Logger.debug('Home page title onAction TapGesture 2 scroller is ' + scroller);
                  scroller.scrollToIndex(0, true);
                }
              }),
            TapGesture({ count: 1 })
              .onAction(() => {
                console.info('TapGesture 1'); // ignore
              })
          ))
      } else {
        Text(HomeConstants.TAB_BAR_TEXTS[this.currentPageIndex])
          .fontColor('#182431')
          .fontSize(HomeConstants.NAV_TITLE_FONT_SIZE_LARGE)
          .lineHeight(41)
          .fontWeight(700)
          .gesture(GestureGroup(GestureMode.Exclusive,
            TapGesture({ count: 2 })
              .onAction(() => {
                console.info('TapGesture 2');
                if (this.currentPageIndex === 0) {
                  let scroller = AppStorage.get(HomeConstants.CHAT_LIST_SCROLLER) as Scroller;
                  Logger.debug('Home page title onAction TapGesture 2 scroller is ' + scroller);
                  scroller.scrollToIndex(0, true);
                }
              }),
            TapGesture({ count: 1 })
              .onAction(() => {
                console.info('TapGesture 1'); // ignore
              })
          ))
      }
      Blank()
    }
    .alignItems(VerticalAlign.Bottom)
    .margin({
      top: HomeConstants.NAV_TITLE_TOP_MARGIN,
      left: HomeConstants.NAV_TITLE_LEFT_MARGIN
    })
  }

  @Builder
  NavigationMenus() {
    if (this.isShowSearchIcon) {
      Row() {
        CircleButton({
          icon: $r('sys.symbol.magnifyingglass_circle'),
        })
      }
      .alignItems(VerticalAlign.Bottom)
      .margin({
        top: HomeConstants.NAV_MENU_TOP_MARGIN,
        left: HomeConstants.NAV_TITLE_LEFT_MARGIN,
        right: HomeConstants.NAV_TITLE_LEFT_MARGIN
      })
      .transition(
        this.searchIconTransitionEffect
      )
    }
  }

  @Builder
  NewMessageSheet() {
    Column() {
      Text('New Message')
        .fontSize(30)
        .fontWeight(700)
        .lineHeight(41)
        .fontColor('#182431')
        .margin({ top: 24, left: 24 })
    }
  }

  @Builder
  PageMap(name: string) {
    if (name === 'ChatDetail') {
      ChatDetail({
        isInChatDetailPage: this.isInChatDetailPage
      });
    } else if (name === "TopicList") {
      ForumTopicList({
        isShowChatListTopPadding: this.isShowChatListTopPadding,
        isShowSearchIcon: this.isShowSearchIcon,
      })
    } else if (name === 'ChatDetailNone') {
      ChatDetailNone();
    } else if (name === 'Login/PhoneNumber') {
      PhoneNumber();
    } else if (name === 'Login/VerifyCode') {
      VerifyCode();
    } else if (name === 'Login/Password') {
      Password();
    } else {
      ChatDetailNone();
    }
  }

  build() {
    Stack({ alignContent: Alignment.Top }) {
      Row() {
        LoadingProgress()
          .size({
            width: HomeConstants.NAV_TITLE_FONT_SIZE,
            height: HomeConstants.NAV_TITLE_FONT_SIZE
          })
          .alignSelf(ItemAlign.Center)
        Text('连接中...')
          .fontSize(HomeConstants.NAV_TITLE_FONT_SIZE)
      }
      .alignItems(VerticalAlign.Center)
      .visibility(this.isLoadingNewMessages && this.currentPageIndex === 0 && this.isOnTop ? Visibility.Visible :
      Visibility.Hidden)
      .zIndex(1)
      .transition(TransitionEffect.move(TransitionEdge.TOP).animation({ curve: curves.springMotion(0.6, 0.8) }))

      Navigation(this.pageInfo) {
        // List() {
        //   ForEach(Array(100).fill("hi"), (item: string) => {
        //     ListItem() {
        //       Text(item)
        //         .fontSize(30)
        //     }
        //   }, (item: string, index) => index + JSON.stringify(item))
        // }
        // .nestedScroll({
        //   scrollForward: NestedScrollMode.PARENT_FIRST,
        //   scrollBackward: NestedScrollMode.SELF_FIRST
        // })
        Tabs({
          barPosition: this.currentBreakpoint === BreakpointConstants.BREAKPOINT_LG ?
          BarPosition.Start : BarPosition.End
        }) {
          TabContent() {
            ChatList({
              isFromHideSearchBar: $isShowChatListTopPadding,
              isShowSearchIcon: $isShowSearchIcon,
              isInTopicPage: false,
            })
              .expandSafeArea([SafeAreaType.SYSTEM])
              .width(BaseConstants.FULL_WIDTH)
            // .height(BaseConstants.FULL_HEIGHT)
          }
          .tabBar(new BottomTabBarStyle({
            normal: HomeConstants.TAB_BAR_ICONS[0],
            selected: HomeConstants.TAB_BAR_SELECTED_ICONS[0],
          }, HomeConstants.TAB_BAR_TEXTS[0]))
          .margin({
            // top: 56
          })
          .expandSafeArea([SafeAreaType.SYSTEM])

          TabContent() {
            // Text(HomeConstants.TAB_BAR_TEXTS[1])
            //   .fontSize(30)
            List() {
              ForEach(Array(100).fill("hi"), (item: string) => {
                ListItem() {
                  Text(item)
                    .fontSize(30)
                }
              }, (item: string, index) => index + JSON.stringify(item))
            }
          }
          .tabBar(new BottomTabBarStyle({
            normal: HomeConstants.TAB_BAR_ICONS[1],
            selected: HomeConstants.TAB_BAR_SELECTED_ICONS[1],
          }, HomeConstants.TAB_BAR_TEXTS[1])
            // .padding({ bottom: HomeConstants.TAB_BAR_PADDING })
          )

          TabContent() {
            Text(HomeConstants.TAB_BAR_TEXTS[2])
              .fontSize(30)

          }
          .tabBar(new BottomTabBarStyle({
            normal: HomeConstants.TAB_BAR_ICONS[2],
            selected: HomeConstants.TAB_BAR_SELECTED_ICONS[2],
          }, HomeConstants.TAB_BAR_TEXTS[2])
            // .padding({ bottom: HomeConstants.TAB_BAR_PADDING })
          )
        }
        .expandSafeArea([SafeAreaType.SYSTEM])
        .scrollable(false)
        .vertical(this.currentBreakpoint === BreakpointConstants.BREAKPOINT_LG)
        .barWidth(this.currentBreakpoint === BreakpointConstants.BREAKPOINT_LG ?
        $r('app.float.bar_width') : BaseConstants.FULL_PERCENT)
        // .barHeight(this.currentBreakpoint === BreakpointConstants.BREAKPOINT_LG ? BaseConstants.FULL_PERCENT :
        //   (deviceInfo.deviceType === BaseConstants.DEVICE_TYPES[0] ? $r('app.float.tab_size_lg') :
        //   $r('app.float.tab_size')))
        .barOverlap(true)
        .barBackgroundColor(this.titleAndTabBarColor)
        .barBackgroundBlurStyle(this.blurStyle)
        .height(BaseConstants.FULL_HEIGHT)
        .width(BaseConstants.FULL_WIDTH)
        .onChange((index: number) => {
          this.currentPageIndex = index;
          Vibrator.kata();
        })
        .animationDuration(300)
        .animation({ curve: curves.springMotion(0.6, 0.8) })

        // .expandSafeArea([SafeAreaType.SYSTEM, SafeAreaType.KEYBOARD], [SafeAreaEdge.TOP, SafeAreaEdge.BOTTOM])
      }
      .bindSheet($$this.isShowNewMessageSheet, this.NewMessageSheet, {
        height: "100%",
        dragBar: false,
        scrollSizeMode: ScrollSizeMode.CONTINUOUS,
        onDisappear: () => {
          this.isShowNewMessageSheet = !this.isShowNewMessageSheet;
        }
      })
      .menus(this.isShowSearchIcon ? this.menuItems2 : this.menuItems)
      .hideTitleBar(true)
      .titleMode(this.titleMode)
      .title(HomeConstants.TAB_BAR_TEXTS[this.currentPageIndex],
        {
          backgroundColor: this.titleAndTabBarColor,
          backgroundBlurStyle: this.blurStyle,
          barStyle: this.barStyle,
        })
      .hideToolBar(true)
      .hideBackButton(true)
      .navBarWidth(this.currentBreakpoint === BreakpointConstants.BREAKPOINT_LG ?
      HomeConstants.NAVIGATION_NAV_BAR_WIDTH_LG :
      HomeConstants.NAVIGATION_NAV_BAR_WIDTH_MD)
      .navDestination(this.PageMap)
      .mode(this.currentBreakpoint === BreakpointConstants.BREAKPOINT_SM ? NavigationMode.Stack :
      NavigationMode.Split)
      .width(BaseConstants.FULL_WIDTH)
      .onAppear(async () => {
        Logger.debug('Home page onAppear');
        let start = Date.now();
        Logger.debug('Home page onAppear registerUpdateChatCallback start');

        Logger.debug('Home page onAppear loadCachedChats start');

        loadCachedChats(this.chatDataSource, this.messagesOfChatMap, this.seenChatsMap)
          .then(async () => {
            let end = Date.now();
            let elapsed = end - start;
            Logger.debug('Home page onAppear loadCachedChats elapsed ' + elapsed + ' ms');
            this.isLoadingNewMessages = true;
            await registerCacheSeenChatCallback(this.cacheSeenChatCallback)
              .catch((e: Error) => {
                Logger.error('Home page onAppear registerCacheSeenChatCallback error ' + e);
              });
            await registerUpdateChatCallback(this.updateChatCallback)
              .catch((e: Error) => {
                Logger.error('Home page onAppear registerUpdateChatCallback error ' + e);
              });
            await registerIncomingMessageCallback(this.incomingMessageCallback)
              .catch((e: Error) => {
                Logger.error('Home page onAppear registerIncomingMessageCallback error ' + e);
              });
            Logger.debug('Home page onAppear loadCachedChats finished');
            isLoggedIn().then((loggedIn) => {
              this.isLoggedIn = loggedIn;

              if (this.isLoggedIn) {
                getMe()// TODO: should cache `me`, say, in preference?
                  .then((me) => {
                    this.me = me;
                  })
                  .catch((e: Error) => {
                    Logger.error('Home page onAppear getMe error ' + e);
                  });
                // this.loadNewMessages()
                //   .then(() => {
                //     this.isLoadingNewMessages = false;
                //     Logger.debug('Home page onAppear loadNewMessages finished');
                //   })
                //   .catch((e: Error) => {
                //     Logger.error('Home page onAppear loadNewMessages error ' + e);
                //   });
                Logger.debug('Home page onAppear isLoggedIn');
                this.isLoadingNewMessages = false;
                run()
                  .catch((e: Error) => {
                    Logger.error('Home page onAppear run error ' + e);
                  });
              } else {
                Logger.debug('Home page onAppear not isLoggedIn');
                this.pageInfo.pushPath({
                  name: 'Login/PhoneNumber',
                });
              }
            })
          })
          .catch((e: Error) => {
            Logger.error('Home page onAppear loadCachedChats error ' + e);
          });
      })
      .animation({ curve: curves.springMotion(0.6, 0.8) })
      .transition(TransitionEffect.move(TransitionEdge.TOP).animation({ curve: curves.springMotion(0.6, 0.8) }))
      // .padding({
      //   top: this.currentBreakpoint === BreakpointConstants.BREAKPOINT_SM ? HomeConstants.NAV_TITLE_TOP_MARGIN : 0
      // })
      .onNavBarStateChange((isVisible: boolean) => {
        Logger.debug('Home page onNavBarStateChange isVisible is ' + isVisible);
        this.isOnTop = isVisible;
        if (isVisible) {
          this.titleMode = NavigationTitleMode.Free;
        } else {
          this.titleMode = NavigationTitleMode.Mini;
        }
      })
      .onTitleModeChange((titleMode) => {
        Logger.debug('Home page onTitleModeChange titleMode is ' + titleMode);
      })
      .customNavContentTransition((from: NavContentInfo, to: NavContentInfo, operation: NavigationOperation) => {
        Logger.debug('Home page customNavContentTransition from ' + from.name + ' to ' + to.name + ' operation ' +
        operation.toString());
        if ((!from || !to) || (!from.name || !to.name)) {
          return undefined;
        }
        // 通过from和to的name对自定义转场路由进行管控
        if (!this.isCustomTransitionEnabled(from.name, to.name)) {
          return undefined;
        }

        // // 需要对转场页面是否注册了animation进行判断，来决定是否进行自定义转场
        // let fromParam: AnimateCallback = CustomTransition.getInstance().getAnimateParam(from.index);
        // let toParam: AnimateCallback = CustomTransition.getInstance().getAnimateParam(to.index);
        // if (!fromParam.animation || !toParam.animation) {
        //   return undefined;
        // }

        return undefined;
      })

      // .expandSafeArea([SafeAreaType.SYSTEM, SafeAreaType.KEYBOARD], [SafeAreaEdge.TOP, SafeAreaEdge.BOTTOM])
    }
    .transition(TransitionEffect.move(TransitionEdge.TOP).animation({ curve: curves.springMotion(0.6, 0.8) }))
    .height(BaseConstants.FULL_HEIGHT)
    .width(BaseConstants.FULL_WIDTH)
  }

  aboutToAppear(): void {
    Logger.debug('Home page aboutToAppear');
    let onForegroundEvent: emitter.InnerEvent = { eventId: 0 };
    emitter.on(onForegroundEvent, async () => {
      Logger.debug('Home page onForegroundEvent');
      let is_authorized = await reconnect().catch((e: Error) => {
        Logger.error('Home page onForegroundEvent reconnect error ' + e);
      });
      if (!is_authorized) {
        Logger.debug('Home page onForegroundEvent reconnect not authorized');
        return;
      }
      // this.loadNewMessages();
      run()
        .catch((err: Error) => {
          Logger.error('Home page onForegroundEvent Failed to homo::tg::run(). Cause: ' + err.name + "-" + err.message);
        });
    })
    emitter.on(EventConstants.onLoginEvent, async () => {
      Logger.debug('Home page onLoginEvent');
      this.isLoggedIn = true;
      this.isLoadingNewMessages = true;
      this.me = await getMe()
        .catch((e: Error) => {
          Logger.error('Home page onLoginEvent getMe error ' + e);
        }) || undefined;
      await this.loadNewMessages()
        .catch((e: Error) => {
          Logger.error('Home page onLoginEvent loadNewMessages error ' + e);
        });
      this.isLoadingNewMessages = false;
      run().catch((e: Error) => {
        Logger.error('Home page onLoginEvent run error ' + e);
      });
    })
  }

  private incomingMessageCallback: IncomingMessageCallback =
    async (err: Error | null, chat: NativeChat, message: NativeMessage) => {
      if (err) {
        Logger.error(`incomingMessageCallback failed, message is ${err.message}`);
        return;
      }
      Logger.debug(`incomingMessageCallback message.text is ${message.text}`);

      if (message) {
        if (!chat) { // chat is null, means chat is already existed
          let existed_chat = this.chatDataSource.get(message.chatId.toString());
          if (existed_chat) {
            existed_chat.lastMessageId = message.messageId;
            existed_chat.lastMessageSenderName = message.senderName;
            existed_chat.lastMessageText = message.text;
            existed_chat.lastMessageTimestamp = message.timestamp;
            cacheOrUpdateChat(existed_chat)
              .catch((e: Error) => {
                Logger.error('incomingMessageCallback cacheOrUpdateChat error ' + e);
              });
            this.messagesOfChatMap.get(message.chatId.toString())?.push(Message.fromNative(message));
            this.chatDataSource.add(existed_chat);
            Logger.debug(`chat exists incomingMessageCallback chat pinned lastMessageText is ${existed_chat.lastMessageText}`);
          } else { // unlikely
            Logger.error(`unlikely incomingMessageCallback chat not exists`);
          }
        } else { // new chat
          let new_chat = Chat.fromNative(chat);
          this.chatDataSource.add(new_chat);
          cacheOrUpdateChat(new_chat)
            .catch((e: Error) => {
              Logger.error('incomingMessageCallback cacheOrUpdateChat error ' + e);
            });
          let messageDataSource = new MessageDataSource();
          messageDataSource.setMessages([Message.fromNative(message)]);
          this.messagesOfChatMap.set(message.chatId.toString(), messageDataSource);
        }
        cacheOrUpdateMessage(Message.fromNative(message))
          .catch((e: Error) => {
            Logger.error('incomingMessageCallback cacheOrUpdateMessage error ' + e);
          });
      }
    };

  private isCustomTransitionEnabled(fromName: string, toName: string): boolean {
    // 点击和返回均需要进行自定义转场，因此需要分别判断
    if ((this.allowedCustomTransitionFromPageName.includes(fromName)
      && this.allowedCustomTransitionToPageName.includes(toName))
      || (this.allowedCustomTransitionFromPageName.includes(toName)
        && this.allowedCustomTransitionToPageName.includes(fromName))) {
      return true;
    }
    return false;
  }

  private cacheSeenChatCallback: (err: Error | null, seenChat: NativeSeenChat) => void =
    async (err: Error | null, seenChat: NativeSeenChat) => {
      if (err) {
        Logger.error(`cacheSeenChatCallback failed, message is ${err.message}`);
        return;
      }
      if (seenChat) {
        this.seenChatsMap.set(seenChat.chatId.toString(), SeenChat.fromNative(seenChat));
        seenChatDao.insertSeenChat(SeenChat.fromNative(seenChat))
          .then(() => {
            Logger.debug('cacheSeenChatCallback insertSeenChat success');
          })
          .catch((e: Error) => {
            Logger.error('cacheSeenChatCallback insertSeenChat error ' + e);
          });
      }
    };

  private async cacheThings(seenChat: NativeSeenChat, chat: NativeChat,
    messages: Message[]) {
    cacheOrUpdateChatPair(SeenChat.fromNative(seenChat), Chat.fromNative(chat))
      .then(() => {
        Logger.debug("updateChatCallback cacheOrUpdateChat success");
      })
      .catch((e: Error) => {
        Logger.error("updateChatCallback cacheOrUpdateChat error " + e);
      });

    cacheOrUpdateMessages(messages)
      .then(() => {
        Logger.debug("updateChatCallback cacheOrUpdateMessages success");
      })
      .catch((e: Error) => {
        Logger.error("updateChatCallback cacheOrUpdateMessages error " + e);
      });
  }

  private async loadNewMessages() {
    this.isLoadingNewMessages = true;
    let lastMessageIds = new Map<string, number>();
    for (let chat of this.chatDataSource.pinnedChats()) {
      lastMessageIds.set(chat.chatId.toString(), chat.lastMessageId);
    }
    for (let chat of this.chatDataSource.sortedUnpinnedChats()) {
      lastMessageIds.set(chat.chatId.toString(), chat.lastMessageId);
    }
    let lastMessageIdsRecord = mapToRecord(lastMessageIds);
    await loadChatsWithOffset(lastMessageIdsRecord)
      .catch((err: Error) => {
        Logger.error('loadNewMessages failed. Cause: ' + err.name + "-" + err.message);
      });
    this.isLoadingNewMessages = false;
    Logger.debug('loadNewMessages finished');
  }
}