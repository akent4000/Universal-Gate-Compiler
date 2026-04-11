import time

def generate_db():
    start_time = time.time()
    
    x0 = 0xAAAA
    x1 = 0xCCCC
    x2 = 0xF0F0
    x3 = 0xFF00
    
    lits_tt = {
        0: 0, 1: 0xFFFF,
        2: x0, 3: (~x0) & 0xFFFF,
        4: x1, 5: (~x1) & 0xFFFF,
        6: x2, 7: (~x2) & 0xFFFF,
        8: x3, 9: (~x3) & 0xFFFF
    }
    
    best_cost = {tt: 9999 for tt in range(65536)}
    # Direct mapping: lit -> (lit_a, lit_b) or None for basics
    lit_to_args = {i: None for i in range(10)} 
    tt_to_lit = {}
    next_lit = 10
    
    for lit, tt in lits_tt.items():
        best_cost[tt] = 0
        tt_to_lit[tt] = lit
        
    pools = {0: list(lits_tt.values())}
    covered = set(lits_tt.values())
    
    for cost in range(1, 16):
        pools[cost] = []
        for c1 in range(cost):
            c2 = (cost - 1) - c1
            if c1 < c2: continue 
            
            for f1 in pools[c1]:
                for f2 in pools[c2]:
                    new_tt = f1 & f2
                    if best_cost[new_tt] > cost:
                        best_cost[new_tt] = cost
                        
                        lit_pos = next_lit; next_lit += 1
                        lit_neg = next_lit; next_lit += 1
                        
                        lit_to_args[lit_pos] = (tt_to_lit[f1], tt_to_lit[f2])
                        lit_to_args[lit_neg] = lit_pos # Negation is just the inverse of pos
                        
                        tt_to_lit[new_tt] = lit_pos
                        tt_to_lit[(~new_tt) & 0xFFFF] = lit_neg
                        
                        pools[cost].append(new_tt)
                        covered.add(new_tt)
                        
                        neg_tt = (~new_tt) & 0xFFFF
                        if best_cost[neg_tt] > cost:
                            best_cost[neg_tt] = cost
                            pools[cost].append(neg_tt)
                            covered.add(neg_tt)
                            
        print(f"Cost {cost}... Covered: {len(covered)} / 65536")
        if len(covered) == 65536:
            break

    print(f"Total time: {time.time() - start_time:.2f}s. Covered: {len(covered)}")
    
    print("Writing db to aig_db_4.py...")
    with open('nand_optimizer/aig_db_4.py', 'w') as f:
        f.write("# Auto-generated Boolean Matching DB\n")
        f.write("AIG_DB_4 = {\n")
        for tt in range(65536):
            if tt not in tt_to_lit: continue
            
            final_lit = tt_to_lit[tt]
            
            ops = []
            visited = {}
            op_idx = 10 
            
            def walk(lit):
                nonlocal op_idx
                if lit < 10:
                    return lit
                
                is_neg = (lit % 2 == 1)
                base_lit = lit - 1 if is_neg else lit
                
                if base_lit in visited:
                    res = visited[base_lit]
                    return res ^ 1 if is_neg else res
                    
                args = lit_to_args[base_lit]
                a_mapped = walk(args[0])
                b_mapped = walk(args[1])
                
                my_idx = op_idx
                op_idx += 2
                
                ops.append((a_mapped, b_mapped))
                visited[base_lit] = my_idx
                
                return my_idx ^ 1 if is_neg else my_idx
                
            out_lit = walk(final_lit)
            
            f.write(f"  {tt}: ({out_lit}, {repr(ops)}),\n")
        f.write("}\n")

if __name__ == '__main__':
    generate_db()

