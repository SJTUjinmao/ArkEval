// ComponentAttrUtils.ets
// 获取组件相对窗口的位置
import { componentUtils, UIContext } from '@kit.ArkUI';
import { JSON } from '@kit.ArkTS';

export class ComponentAttrUtils {
  // 根据组件的id获取组件的位置信息
  public static getRectInfoById(context: UIContext, id: string): RectInfoInPx {
    if (!context || !id) {
      throw Error('object is empty');
    }
    let componentInfo: componentUtils.ComponentInfo = context.getComponentUtils().getRectangleById(id);

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

export class RectInfoInPx {
  left: number = 0;
  top: number = 0;
  right: number = 0;
  bottom: number = 0;
  width: number = 0;
  height: number = 0;
}

export class RectJson {
  $rect: Array<number> = [];
}