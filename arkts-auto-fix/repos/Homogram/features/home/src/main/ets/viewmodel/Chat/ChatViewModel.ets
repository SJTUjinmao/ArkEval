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

import { Want, common } from '@kit.AbilityKit';
import { BusinessError } from '@kit.BasicServicesKit';
import { hilog } from '@kit.PerformanceAnalysisKit';
import { Logger, Database, stringToBuffer, delay, typedArrayToBuffer, SortedHashMap } from '@ohos/base';
import {
  NativeMessage,
  NativeChat,
  ChatType,
  NativePackedChat,
  NativeRawMessage,
  NativeSeenChat
} from 'libhomogrape.so';
import { DatabaseConstants } from '../../constants/DatabaseConstants';
import { syncCachesFromLocalDb } from 'libhomogrape.so'
import { Chat } from '../../entities/Chat';
import { Message } from '../../entities/Message';
import { PackedChat } from '../../entities/PackedChat';
import { RawMessage } from '../../entities/RawMessage';
import { chatDao } from '../../dao/ChatDao';
import { messageDao } from '../../dao/MessageDao';
import { packedChatDao } from '../../dao/PackedChatDao';
import { rawMessageDao } from '../../dao/RawMessageDao';
import { ChatDataSource } from './ChatDataSource';
import { MessageDataSource } from '../Message/MessageDataSource';
import { HashMap } from '@kit.ArkTS';
import { seenChatDao } from '../../dao/SeenChatDao';
import { SeenChat } from '../../entities/SeenChat';

export type UpdateChatCallback = (err: Error | null, seenChat: NativeSeenChat, chat: NativeChat,
  messages: NativeMessage[]) => Promise<void>;

export type IncomingMessageCallback = (err: Error | null, chat: NativeChat, message: NativeMessage) => void;

// load locally cached chats from database and sync to the rust side
// we should separate loading chats and messages, because loading messages is a heavy task
export async function loadCachedChats(chatDataSource: ChatDataSource,
  messagesOfChats: HashMap<string, MessageDataSource>, seenChatsMap: HashMap<string, SeenChat>): Promise<void> {
  Logger.debug('loadCachedChats: loading cached chats');
  let start = 0;
  let end = 0;
  let elapsed = 0;
  start = Date.now();

  // let pinnedChats: Chat[] = await chatDao.getAllPinnedChats();
  // Logger.debug('loadCachedChats: retrieved ' + pinnedChats.length + ' pinned chats');
  // let unpinnedChats: Chat[] = await chatDao.getAllUnpinnedChats();
  // Logger.debug('loadCachedChats: retrieved ' + unpinnedChats.length + ' unpinned chats');
  let seen_chats = await seenChatDao.getAllSeenChats();
  for (let i = 0; i < seen_chats.length; i++) {
    seenChatsMap.set(seen_chats[i].chatId.toString(), seen_chats[i]);
  }
  Logger.debug('loadCachedChats: retrieved ' + seen_chats.length + ' seen chats');

  let chats = await chatDao.getAllChats();
  Logger.debug('loadCachedChats: retrieved ' + chats.length + ' chats');

  await chatDataSource.addChatsFromSingleArray(chats);

  end = Date.now();
  elapsed = end - start;

  Logger.debug('loadCachedChats: getAllChats and addChatsFromSingleArray elapsed ' + elapsed + ' ms');

  start = Date.now();


  for (let i = 0; i < chats.length; i++) {
    let messages = await messageDao.getMessagesByChatId(chats[i].chatId);
    let messageDataSources = messagesOfChats.get(chats[i].chatId.toString()) || new MessageDataSource();
    messageDataSources.setMessages(messages);
    messagesOfChats.set(chats[i].chatId.toString(), messageDataSources);
    // packed_chats.push((await packedChatDao.getPackedChatById(chats[i].chatId))); // TODO: using batch query
    // if (packed_chats[packed_chats.length - 1] === null) {
    //   Logger.error('loadCachedChats: packed chat not found for chat ' + chats[i].chatId);
    //   continue;
    // }
  }
  end = Date.now();
  elapsed = end - start;
  Logger.debug('loadCachedChats: before syncCachesFromLocalDb elapsed ' + elapsed + ' ms');

  let nativePackedChats: NativePackedChat[] = await Promise.all(seen_chats.map(async (value) => {
    let ret: NativePackedChat = {
      chatId: value.chatId,
      packedChat: value.packedChat,
    };
    return ret;
  }));
  let nativeChats = await Promise.all(chats.map(async (value) => {
    return value.toNative();
  }))
  syncCachesFromLocalDb(nativePackedChats, nativeChats)
    .catch((error: Error) => {
      Logger.error('loadCachedChats: syncCachesFromLocalDb failed: ' + error);
    });

  Logger.debug('loadCachedChats: loaded ' + chats.length + ' chats');
}

export async function cacheOrUpdateChat(chat: Chat): Promise<void> {
  await chatDao.insertChat(chat);
}

export async function cacheOrUpdateChatPair(seenChat: SeenChat, chat: Chat): Promise<void> {
  await chatDao.insertChat(chat); // this need to be awaited due to the chat_id foreign key of the following tables
  seenChatDao.insertSeenChat(seenChat);
}

export async function cacheOrUpdateMessages(messages: Message[]): Promise<void> {
  await messageDao.insertMessages(messages); // this need to be awaited due to the message_id foreign key of raw_messages
}

export async function cacheOrUpdateMessage(message: Message): Promise<void> {
  messageDao.insertMessage(message);
}

class ChatViewModel {
  public startAppletAbility(context: common.UIAbilityContext, index: number): void {
    const want: Want = {
      bundleName: getContext(context).applicationInfo.name,
      abilityName: 'AppletAbility',
      parameters: {
        position: index
      }
    };
    try {
      context.startAbility(want, (err: BusinessError) => {
        if (err.code) {
          Logger.error(`startAbility failed, code is ${err.code}, message is ${err.message}`);
        }
      });
    } catch (error) {
      hilog.error(0x00004, 'ChatViewModel', '%{public}s', error);
    }
  }

  public startDocumentAbility(context: common.UIAbilityContext, index: number): void {
    const want: Want = {
      bundleName: getContext(context).applicationInfo.name,
      abilityName: 'DocumentAbility',
      parameters: {
        position: index
      }
    };
    try {
      context.startAbility(want, (err: BusinessError) => {
        if (err.code) {
          Logger.error(`startAbility failed, code is ${err.code}, message is ${err.message}`);
        }
      });
    } catch (error) {
      Logger.error(`startAbility failed,  message is ${error}`);
    }
  }
}

export { ChatViewModel };