import { BaseConstants, BreakpointConstants } from '@ohos/base';
import { HomeConstants } from '../../../constants/HomeConstants';
import { ChatDataSource } from '../../../viewmodel/Chat/ChatDataSource';
import { ChatList } from '../../../views/Chat/ChatList';

@Component
export struct ForumTopicList {
  @StorageProp('currentBreakpoint') currentBreakpoint: string = BreakpointConstants.BREAKPOINT_SM;
  @Consume('pageInfo') pageInfo: NavPathStack;
  @Consume chatDataSource: ChatDataSource;
  @Link isShowChatListTopPadding: boolean;
  @Link isShowSearchIcon: boolean;

  build() {
    NavDestination() {
      Flex() {
        Column() {
          ChatList({
            isFromHideSearchBar: $isShowChatListTopPadding,
            isShowSearchIcon: $isShowSearchIcon,
            isInTopicPage: true,
          })// .width(BaseConstants.FULL_WIDTH)
            .height(BaseConstants.FULL_HEIGHT)
            .width(HomeConstants.FORUM_TOPIC_LIST_PHOTO_SIZE)
        }

        Column() {
          Text("hola")
        }
      }
    }
  }
}