import sql from "@liushengyi/smartdb"
import { NativePackedChat } from 'libhomogrape.so'

export class PackedChat implements NativePackedChat {
  @sql.SqlColumn(sql.ColumnType.INTEGER)
  chatId: number;
  @sql.SqlColumn(sql.ColumnType.TEXT)
  packedChat: string;

  constructor(chatId: number, packedChat: string) {
    this.chatId = chatId;
    this.packedChat = packedChat;
  }

  public static fromNative(native: NativePackedChat): PackedChat {
    return new PackedChat(native.chatId, native.packedChat);
  }

  public toNative(): NativePackedChat {
    return {
      chatId: this.chatId,
      packedChat: this.packedChat
    };
  }
}