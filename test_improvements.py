"""
测试新的错误处理和数据库连接池

运行方式:
    python test_improvements.py
"""

import os
import sys
import tempfile
import sqlite3

# 将项目路径添加到 sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core_utils import (
    DatabasePool,
    DatabaseError,
    handle_errors,
    db_operation,
    retry_on_error,
    success_response,
    error_response,
    BookingError,
    ResourceAlreadyBookedError,
)


def test_database_pool():
    """测试数据库连接池的线程安全性"""
    print("\n=== 测试数据库连接池 ===")
    
    # 使用临时数据库
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as tmp:
        db_path = tmp.name
    
    try:
        # 创建连接池
        pool = DatabasePool(db_path)
        
        # 创建测试表
        with pool.get_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS test_bookings (
                    id INTEGER PRIMARY KEY,
                    username TEXT NOT NULL,
                    bookdate TEXT NOT NULL,
                    UNIQUE(username, bookdate)
                )
            """)
        
        # 测试插入
        with pool.get_connection() as conn:
            conn.execute("INSERT INTO test_bookings (username, bookdate) VALUES (?, ?)", 
                        ("user1", "2026-01-10"))
        
        # 测试 UNIQUE 约束
        try:
            with pool.get_connection() as conn:
                conn.execute("INSERT INTO test_bookings (username, bookdate) VALUES (?, ?)", 
                            ("user1", "2026-01-10"))
            print("❌ 应该触发 UNIQUE 约束错误")
        except sqlite3.IntegrityError:
            print("✅ UNIQUE 约束正常工作")
        
        # 测试查询
        with pool.get_connection(auto_commit=False) as conn:
            cur = conn.execute("SELECT * FROM test_bookings")
            rows = cur.fetchall()
            print(f"✅ 查询成功，返回 {len(rows)} 条记录")
        
        # 清理
        pool.close_all()
        print("✅ 数据库连接池测试通过")
        
    finally:
        # 删除临时文件
        if os.path.exists(db_path):
            os.remove(db_path)


def test_error_handling():
    """测试统一错误处理"""
    print("\n=== 测试错误处理装饰器 ===")
    
    @handle_errors(default_return={"status": "error"}, log_error=False)
    def risky_operation(should_fail: bool):
        if should_fail:
            raise ValueError("模拟失败")
        return {"status": "success"}
    
    # 测试成功情况
    result = risky_operation(False)
    assert result["status"] == "success", "成功情况测试失败"
    print("✅ 成功情况处理正确")
    
    # 测试失败情况
    result = risky_operation(True)
    assert result["status"] == "error", "失败情况测试失败"
    print("✅ 失败情况处理正确（返回默认值）")


def test_db_operation_decorator():
    """测试数据库操作装饰器"""
    print("\n=== 测试数据库操作装饰器 ===")
    
    @db_operation
    def insert_duplicate():
        # 模拟 IntegrityError
        raise sqlite3.IntegrityError("UNIQUE constraint failed")
    
    try:
        insert_duplicate()
        print("❌ 应该抛出 DatabaseError")
    except DatabaseError as e:
        print(f"✅ 正确捕获并转换异常: {e.code}")


def test_custom_exceptions():
    """测试自定义异常"""
    print("\n=== 测试自定义异常 ===")
    
    try:
        raise ResourceAlreadyBookedError(
            message="该时间段已被 user1 预约",
            details={"username": "user1", "bookdate": "2026-01-10"}
        )
    except BookingError as e:
        print(f"✅ 异常代码: {e.code}")
        print(f"✅ 异常消息: {e.message}")
        print(f"✅ 异常详情: {e.details}")
        
        # 测试响应格式化
        response = error_response(e)
        assert response["ok"] is False
        assert response["error"] == "RESOURCE_ALREADY_BOOKED"
        print("✅ 错误响应格式正确")


def test_retry_decorator():
    """测试重试装饰器"""
    print("\n=== 测试重试装饰器 ===")
    
    attempt_count = {"count": 0}
    
    @retry_on_error(max_retries=3, delay=0.1, exceptions=(ValueError,))
    def flaky_operation():
        attempt_count["count"] += 1
        if attempt_count["count"] < 3:
            raise ValueError(f"第 {attempt_count['count']} 次尝试失败")
        return "成功"
    
    result = flaky_operation()
    assert result == "成功", "重试测试失败"
    assert attempt_count["count"] == 3, "重试次数不正确"
    print(f"✅ 重试 {attempt_count['count']} 次后成功")


def run_all_tests():
    """运行所有测试"""
    print("\n" + "="*50)
    print("  核心工具模块测试")
    print("="*50)
    
    try:
        test_database_pool()
        test_error_handling()
        test_db_operation_decorator()
        test_custom_exceptions()
        test_retry_decorator()
        
        print("\n" + "="*50)
        print("  ✅ 所有测试通过！")
        print("="*50)
        
    except AssertionError as e:
        print(f"\n❌ 测试失败: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ 测试异常: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    run_all_tests()
