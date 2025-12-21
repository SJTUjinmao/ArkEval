import { componentUtils } from '@kit.ArkUI';
import { BaseConstants, StandardIcon } from '@ohos/base';
import { HomeConstants } from '../../constants/HomeConstants';
import { Chat } from '../../entities/Chat';
import { ChatDetailBottomPhotoPicker } from './ChatDetailBottomPhotoPicker';
import { HashMap } from '@kit.ArkTS';
import { ChatDataSource } from '../../viewmodel/Chat/ChatDataSource';
import { MessageDataSource } from '../../viewmodel/Message/MessageDataSource';

@Entry
@Component
export struct ChatDetailBottomMediaSheet {
  // @Consume pinnedChatsMap: HashMap<string, Chat>;
  // @Consume chatsMap: Map<string, Chat>;
  @Consume chatDataSource: ChatDataSource;
  @Consume messagesOfChatMap: HashMap<string, MessageDataSource>;
  @State tabArray: Array<number> = [0, 1, 2, 3, 4];
  @State tabArrayVal: number = this.tabArray.length - 1;
  @State titleArray: Array<Resource> = [
    $r('app.string.gallery'),
    $r('app.string.file'),
    $r('app.string.location'),
    $r('app.string.poll'),
    $r('app.string.contact')
  ];
  @State currentIndex: number = 0;
  @State animationDuration: number = 300;
  @State indicatorLeftMargin: number = 0;
  @State indicatorWidth: number = 0;
  @Link selectedMediaCount: number;
  @Link animate: boolean;
  @Link text: string;
  @Link title: Resource;
  @Link selectedMedias: Array<string>;
  @Prop currentDetents: number;
  private controller: TabsController = new TabsController();
  private tabsWidth: number = 0;

  @Builder
  MediaBar() {
    Flex() {
      Column() {
        TextArea({
          text: this.text,
        })
          .placeholderColor($r('app.color.text_input_default_color'))
          .caretColor($r('app.color.text_input_default_care_color'))
          .backgroundColor($r('app.color.text_input_default_background_color'))
          .borderRadius(HomeConstants.CHAT_DETAIL_BOTTOM_TEXT_RADIUS)
          .flexGrow(BaseConstants.FLEX_GROW_ONE)
          .padding({ right: $r('app.float.chat_detail_bottom_text_padding') })
          .backgroundColor(Color.White)
          .enterKeyType(EnterKeyType.Send)
          .onChange((text: string) => {
            this.text = text;
          })
          .onSubmit((enterKey: EnterKeyType) => {
            if (enterKey == EnterKeyType.Send) {
              this.sendMessage();
            }
          })
      }

      Column() {
        StandardIcon({ icon: $r('app.media.ic_public_email_send') })
      }
      .flexBasis(HomeConstants.FLEX_BASIS_AUTO)
      .padding({
        left: $r('app.float.chat_detail_bottom_padding'),
        right: $r('app.float.chat_detail_bottom_padding'),
      })
      .onClick(() => {
        this.sendMessage();
      })
    }
    .margin({
      top: 10,
    })
  }

  // 单独的页签
  @Builder
  tab(tabName: Resource, tabItem: number, tabIndex: number) {
    Row({ space: 20 }) {
      Text(tabName).fontSize(18)
        .fontColor(tabItem === this.currentIndex ? Color.Red : Color.Black)
        .id(tabIndex.toString())
        .onAreaChange((oldValue: Area, newValue: Area) => {
          if (this.currentIndex === tabIndex && (this.indicatorLeftMargin === 0 || this.indicatorWidth === 0)) {
            if (newValue.position.x !== undefined) {
              let positionX = Number.parseFloat(newValue.position.x.toString());
              this.indicatorLeftMargin = Number.isNaN(positionX) ? 0 : positionX;
            }
            let width = Number.parseFloat(newValue.width.toString());
            this.indicatorWidth = Number.isNaN(width) ? 0 : width;
          }
        })
    }
    .justifyContent(FlexAlign.Center)
    .constraintSize({ minWidth: 35 })
    .width(80)
    .height(35)
    .borderRadius({
      topLeft: 10,
      topRight: 10
    })
    .onClick(() => {
      this.controller.changeIndex(tabIndex);
      this.currentIndex = tabIndex;
    })
  }

  build() {
    Flex({ direction: FlexDirection.Column }) {
      Tabs({ barPosition: BarPosition.Start, controller: this.controller }) {
        TabContent() {
          ChatDetailBottomPhotoPicker({
            selectedMediaCount: $selectedMediaCount,
            animate: $animate
          })
        }

        ForEach(this.tabArray.slice(1), (item: number, index: number) => {
          TabContent() {
            Text(this.titleArray[index + 1])
              .width('100%')
              .fontSize(30)
              .textAlign(TextAlign.Center)
          }
          .backgroundColor(Color.Pink)
        })
      }
      .onAreaChange((oldValue: Area, newValue: Area) => {
        let width = Number.parseFloat(newValue.width.toString());
        this.tabsWidth = Number.isNaN(width) ? 0 : width;
      })
      .barWidth('100%')
      .barHeight(0)
      .width('100%')
      // .height('85%')
      .backgroundColor('#F1F3F5')
      .animationDuration(this.animationDuration)
      .onChange((index: number) => {
        this.currentIndex = index; // 监听索引index的变化，实现页签内容的切换。
        this.title = this.titleArray[index];
      })
      .onAnimationStart((index: number, targetIndex: number, event: TabsAnimationEvent) => {
        // 切换动画开始时触发该回调。下划线跟着页面一起滑动，同时宽度渐变。
        this.currentIndex = targetIndex;
        let targetIndexInfo = this.getTextInfo(targetIndex);
        this.startAnimateTo(this.animationDuration, targetIndexInfo.left, targetIndexInfo.width);
      })
      .onAnimationEnd((index: number, event: TabsAnimationEvent) => {
        // 切换动画结束时触发该回调。下划线动画停止。
        let currentIndicatorInfo = this.getCurrentIndicatorInfo(index, event);
        this.startAnimateTo(0, currentIndicatorInfo.left, currentIndicatorInfo.width);
      })
      .onGestureSwipe((index: number, event: TabsAnimationEvent) => {
        // 在页面跟手滑动过程中，逐帧触发该回调。
        let currentIndicatorInfo = this.getCurrentIndicatorInfo(index, event);
        this.currentIndex = currentIndicatorInfo.index;
        this.indicatorLeftMargin = currentIndicatorInfo.left;
        this.indicatorWidth = currentIndicatorInfo.width;
      })

      Column() {
        if (this.selectedMediaCount > 0) {
          this.MediaBar()
        } else {
          Stack({ alignContent: Alignment.TopStart }) {

            Flex() {
              ForEach(this.tabArray, (item: number, index: number) => {
                this.tab(this.titleArray[item], item, index);
              })
            }
            .width('100%')

            Column()
              .width(this.indicatorWidth)
              .height(2)
              .backgroundColor(Color.Red)
              .borderRadius(2)
              .margin({
                left: this.indicatorLeftMargin,
                top: 38
              })
          }
        }
      }
      .width('100%')
      .height('80vp')
    }
    .height('100%')
  }

  private sendMessage: () => void = () => {
  }; // placeholder

  // 获取组件大小、位置、平移缩放旋转及仿射矩阵属性信息。
  private getTextInfo(index: number): Record<string, number> {
    let modePosition: componentUtils.ComponentInfo = componentUtils.getRectangleById(index.toString());
    return { 'left': px2vp(modePosition.windowOffset.x), 'width': px2vp(modePosition.size.width) };
  }

  private getCurrentIndicatorInfo(index: number, event: TabsAnimationEvent): Record<string, number> {
    let nextIndex = index;
    if (index > 0 && event.currentOffset > 0) {
      nextIndex--;
    } else if (index < 3 && event.currentOffset < 0) {
      nextIndex++;
    }
    let indexInfo = this.getTextInfo(index);
    let nextIndexInfo = this.getTextInfo(nextIndex);
    let swipeRatio = Math.abs(event.currentOffset / this.tabsWidth);
    // 页面滑动超过一半，tabBar切换到下一页。
    let currentIndex = swipeRatio > 0.5 ? nextIndex : index;
    let currentLeft = indexInfo.left + (nextIndexInfo.left - indexInfo.left) * swipeRatio;
    let currentWidth = indexInfo.width + (nextIndexInfo.width - indexInfo.width) * swipeRatio;
    return { 'index': currentIndex, 'left': currentLeft, 'width': currentWidth };
  }

  private startAnimateTo(duration: number, leftMargin: number, width: number) {
    animateTo({
      // 动画时长
      duration: duration,
      // 动画曲线
      curve: Curve.Linear,
      // 播放次数
      iterations: 1,
      // 动画模式
      playMode: PlayMode.Normal,
      onFinish: () => {
        console.info('play end');
      }
    }, () => {
      this.indicatorLeftMargin = leftMargin;
      this.indicatorWidth = width;
    })
  }
}