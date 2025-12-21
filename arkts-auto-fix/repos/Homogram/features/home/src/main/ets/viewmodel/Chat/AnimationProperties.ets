// AnimationProperties.ets
// 一镜到底转场动画封装
import { componentUtils, curves } from '@kit.ArkUI';
import { RectInfoInPx } from '@ohos/base';
import { WindowUtil } from '@ohos/base';
import { ChatPhotoNodeController } from './ChatPhotoNodeController';

const TAG: string = 'AnimationProperties';

const DEVICE_BORDER_RADIUS: number = 34;

@Observed
class AnimationProperties {
  public isExpandPageShow: boolean = false;
  // 控制组件是否响应点击事件
  public isEnabled: boolean = true;
  // 展开卡片的序号
  public curIndex: number = -1;
  public translateX: number = 0;
  public translateY: number = 0;
  public positionX: number = 0;
  public positionY: number = 0;
  public changedHeight: boolean = false;
  private calculatedTranslateX: number = 0;
  private calculatedTranslateY: number = 0;
  // 设置卡片展开后相对父组件的位置
  private expandTranslateX: number = 0;
  private expandTranslateY: number = 0;

  public expandAnimation(index: number): void {
    // 记录展开态卡片的序号
    if (index != undefined) {
      this.curIndex = index;
    }
    // 计算折叠态卡片相对父组件的位置
    this.calculateData(index.toString());
    // 展开态卡片上树
    this.isExpandPageShow = true;
    // 卡片展开的属性动画
    animateTo({
      curve: curves.springMotion(0.6, 0.9)
    }, () => {
      this.translateX = this.calculatedTranslateX;
      this.translateY = this.calculatedTranslateY;
      this.changedHeight = true;
    })
  }

  // 获取需要跨节点迁移的组件的位置，及迁移前后节点的公共父节点的位置，用以计算做动画组件的动画参数
  public calculateData(key: string): void {
    let clickedImageInfo = this.getRectInfoById(key);
    let rootStackInfo = this.getRectInfoById('rootStack');
    this.positionX = px2vp(clickedImageInfo.left - rootStackInfo.left);
    this.positionY = px2vp(clickedImageInfo.top - rootStackInfo.top);
    this.calculatedTranslateX = px2vp(rootStackInfo.left - clickedImageInfo.left) + this.expandTranslateX;
    this.calculatedTranslateY = px2vp(rootStackInfo.top - clickedImageInfo.top) + this.expandTranslateY;
  }

  // 根据组件的id获取组件的位置信息
  private getRectInfoById(id: string): RectInfoInPx {
    let componentInfo: componentUtils.ComponentInfo = componentUtils.getRectangleById(id);

    if (!componentInfo) {
      throw Error('object is empty');
    }

    let rstRect: RectInfoInPx = new RectInfoInPx();
    const widthScaleGap = componentInfo.size.width * (1 - componentInfo.scale.x) / 2;
    const heightScaleGap = componentInfo.size.height * (1 - componentInfo.scale.y) / 2;
    rstRect.left = componentInfo.translate.x + componentInfo.windowOffset.x + widthScaleGap;
    rstRect.top = componentInfo.translate.y + componentInfo.windowOffset.y + heightScaleGap;
    rstRect.right =
      componentInfo.translate.x + componentInfo.windowOffset.x + componentInfo.size.width - widthScaleGap;
    rstRect.bottom =
      componentInfo.translate.y + componentInfo.windowOffset.y + componentInfo.size.height - heightScaleGap;
    rstRect.width = rstRect.right - rstRect.left;
    rstRect.height = rstRect.bottom - rstRect.top;

    return {
      left: rstRect.left,
      right: rstRect.right,
      top: rstRect.top,
      bottom: rstRect.bottom,
      width: rstRect.width,
      height: rstRect.height
    }
  }
}