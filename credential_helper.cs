// Codex 通过官方 auth.command 调用此程序；只向调用方管道输出解密后的 Key。
// 密文由 Windows 当前用户 DPAPI 保护，不依赖明文配置或用户环境变量。
using System;
using System.IO;
using System.Security.Cryptography;
using System.Text;

internal static class CredentialReader
{
    private static int Main(string[] args)
    {
        try
        {
            if (args.Length != 1 || !Path.IsPathRooted(args[0])) return 2;
            byte[] plain = ProtectedData.Unprotect(File.ReadAllBytes(args[0]), null, DataProtectionScope.CurrentUser);
            string token = Encoding.UTF8.GetString(plain);
            if (string.IsNullOrWhiteSpace(token) || token.Contains("\n") || token.Contains("\r")) return 3;
            Console.OutputEncoding = new UTF8Encoding(false);
            Console.Write(token);
            Array.Clear(plain, 0, plain.Length);
            return 0;
        }
        catch
        {
            Console.Error.Write("ModelBridge credential unavailable for this Windows user.");
            return 1;
        }
    }
}
