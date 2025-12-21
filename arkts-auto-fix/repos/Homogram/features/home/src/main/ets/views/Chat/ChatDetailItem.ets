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
import { ChatType } from 'libhomogrape.so';

import { HomeConstants } from '../../constants/HomeConstants';
import { Message } from '../../entities/Message';
import { MessageBubble } from '../Message/MessageBubble';

@Component
export default struct ChatDetailItem {
  private messageListScroller?: ListScroller;
  private chatType?: ChatType;
  private message?: Message;
  private isAppletMsg?: boolean;
  private isDocumentMsg?: boolean;

  build() {
    Flex({ justifyContent: FlexAlign.End, alignItems: ItemAlign.End }) {
      MessageBubble({
        chatType: this.chatType,
        content: this.message?.text,
        isAppletMsg: this.isAppletMsg,
        isDocumentMsg: this.isDocumentMsg,
        message: this.message,
        messageListScroller: this.messageListScroller,
      })
    }
  }
}